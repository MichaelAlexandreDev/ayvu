"""Consent to remote egress, recorded in the project database (#379).

One row per project and remote provider: the provider, when the owner agreed (UTC) and the version
of the text they agreed to. A row whose version is not the current text no longer counts, so a
changed text is asked again. Revoking deletes the rows; nothing else in the project changes.
The table is project schema version 5, and a released step is never edited.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from ..kernel import JobError, is_slug, validate_utc
from ..kernel.sqlite import MigrationError, SqliteMigrationRunner, sqlite_failure_code
from .model import ProjectError

# The version of the consent text the CLI shows. A new text is a new version, and it is asked again.
REMOTE_EGRESS_TEXT_VERSION = "remote-egress-v1"
MAX_CONSENTS = 64

REMOTE_CONSENT_SCHEMA_STATEMENTS = (
    """CREATE TABLE remote_egress_consents (
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    granted_at TEXT NOT NULL,
    text_version TEXT NOT NULL CHECK (length(text_version) BETWEEN 1 AND 64),
    PRIMARY KEY (project_id, provider)
) WITHOUT ROWID""",
)


class RemoteConsentError(ProjectError):
    """A bounded code; the message never carries a path or a value from the database."""


@dataclass(frozen=True)
class RemoteConsent:
    provider: str
    granted_at: datetime
    text_version: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_provider(provider: str) -> None:
    if not is_slug(provider):
        raise RemoteConsentError("invalid_provider")


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except RemoteConsentError:
        raise
    except MigrationError as error:
        raise RemoteConsentError(error.code) from None
    except (sqlite3.Error, ValueError) as error:
        raise RemoteConsentError(sqlite_failure_code(error) if isinstance(error, sqlite3.Error)
                                 else "invalid_consent_record") from None
    except OSError:
        raise RemoteConsentError("io") from None


class SqliteRemoteConsentRepository:
    """Consents of one project. The database must already be at the runner's target schema."""

    def __init__(self, runner: SqliteMigrationRunner, database: Path, project_id: str, *,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        if not isinstance(project_id, str) or not project_id or len(project_id) > 64:
            raise RemoteConsentError("invalid_project")
        self._runner, self._database, self._project_id, self._clock = runner, database, project_id, clock

    def __repr__(self) -> str:
        return f"SqliteRemoteConsentRepository(project_id={self._project_id!r})"

    @contextmanager
    def _connection(self, *, readonly: bool) -> Iterator[sqlite3.Connection]:
        with _translated_errors(), closing(self._runner.connect(self._database, readonly=readonly)) as connection:
            connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            try:
                yield connection
                if not readonly:
                    connection.execute("COMMIT")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")

    def list(self) -> tuple[RemoteConsent, ...]:
        with self._connection(readonly=True) as connection:
            rows = connection.execute(
                "SELECT provider, granted_at, text_version FROM remote_egress_consents WHERE project_id = ? "
                "ORDER BY provider LIMIT ?", (self._project_id, MAX_CONSENTS)).fetchall()
            return tuple(RemoteConsent(row[0], datetime.fromisoformat(row[1]), row[2]) for row in rows)

    def current(self, provider: str, text_version: str = REMOTE_EGRESS_TEXT_VERSION) -> RemoteConsent | None:
        """The consent to this exact text, or None: an older text's row does not count."""
        _check_provider(provider)
        found = [item for item in self.list() if item.provider == provider and item.text_version == text_version]
        return found[0] if found else None

    def grant(self, provider: str, text_version: str = REMOTE_EGRESS_TEXT_VERSION) -> RemoteConsent:
        """Record (or renew) consent now. Only the provider, the UTC time and the text version are stored."""
        _check_provider(provider)
        if not isinstance(text_version, str) or not 1 <= len(text_version) <= 64:
            raise RemoteConsentError("invalid_text_version")
        granted_at = self._clock()
        try:
            validate_utc(granted_at)
        except JobError:
            raise RemoteConsentError("invalid_timestamp") from None
        with self._connection(readonly=False) as connection:
            connection.execute(
                "INSERT INTO remote_egress_consents (project_id, provider, granted_at, text_version) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (project_id, provider) DO UPDATE SET granted_at = excluded.granted_at, "
                "text_version = excluded.text_version",
                (self._project_id, provider, granted_at.isoformat(timespec="microseconds"), text_version))
        return RemoteConsent(provider, granted_at, text_version)

    def revoke(self, provider: str | None = None) -> int:
        """Delete this project's consent for ``provider``, or every one of them. Returns how many were removed."""
        if provider is not None:
            _check_provider(provider)
        with self._connection(readonly=False) as connection:
            if provider is None:
                cursor = connection.execute("DELETE FROM remote_egress_consents WHERE project_id = ?",
                                            (self._project_id,))
            else:
                cursor = connection.execute(
                    "DELETE FROM remote_egress_consents WHERE project_id = ? AND provider = ?",
                    (self._project_id, provider))
            return cursor.rowcount
