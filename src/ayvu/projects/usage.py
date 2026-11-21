"""Provider usage, recorded in the project database (#380): one row per provider request, never any text.

A row holds the request's job, document and unit, the provider and model, the characters and words sent,
the tokens the provider reported and the cost in micro-dollars, with the price table that priced it. An
unknown count or cost is NULL, never zero. The request id is the key, so a request is counted once,
whichever worker records it; a resume that replays earlier results sends nothing and records nothing.
Provenance records are not changed: a row joins them through its job and document. The table is project
schema version 6, and a released step is never edited.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from ..kernel.sqlite import MigrationError, SqliteMigrationRunner, sqlite_failure_code
from ..providers import UsageRecord, UsageTotals
from .model import ProjectError

MAX_JOBS_PER_TOTAL = 64

PROVIDER_USAGE_SCHEMA_STATEMENTS = (
    """CREATE TABLE provider_usage (
    request_id TEXT PRIMARY KEY CHECK (length(request_id) BETWEEN 1 AND 64),
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 64),
    document_id TEXT NOT NULL CHECK (length(document_id) BETWEEN 1 AND 128),
    unit_id TEXT NOT NULL CHECK (length(unit_id) BETWEEN 1 AND 128),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model TEXT CHECK (model IS NULL OR length(model) BETWEEN 1 AND 256),
    characters INTEGER NOT NULL CHECK (characters >= 0),
    words INTEGER NOT NULL CHECK (words >= 0),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cost_micro_usd INTEGER CHECK (cost_micro_usd IS NULL OR cost_micro_usd >= 0),
    price_version TEXT CHECK (price_version IS NULL OR length(price_version) BETWEEN 1 AND 32),
    price_source TEXT CHECK (price_source IS NULL OR price_source IN ('table', 'config')),
    recorded_at TEXT NOT NULL
)""",
    "CREATE INDEX provider_usage_by_job ON provider_usage(project_id, job_id)",
)

_COLUMNS = ("request_id", "job_id", "document_id", "unit_id", "provider", "model", "characters", "words",
            "input_tokens", "output_tokens", "cached_input_tokens", "cost_micro_usd", "price_version", "price_source")


class ProviderUsageError(ProjectError):
    """A bounded code; the message never carries a path or a value from the database."""


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except ProviderUsageError:
        raise
    except MigrationError as error:
        raise ProviderUsageError(error.code) from None
    except sqlite3.Error as error:
        raise ProviderUsageError(sqlite_failure_code(error)) from None
    except (OSError, ValueError):
        raise ProviderUsageError("io") from None


class SqliteProviderUsageRepository:
    """Usage of one project. The database must already be at the runner's target schema."""

    def __init__(self, runner: SqliteMigrationRunner, database: Path, project_id: str, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not isinstance(project_id, str) or not project_id or len(project_id) > 64:
            raise ProviderUsageError("invalid_project")
        self._runner, self._database, self._project_id, self._clock = runner, database, project_id, clock

    def __repr__(self) -> str:
        return f"SqliteProviderUsageRepository(project_id={self._project_id!r})"

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

    def record(self, record: UsageRecord) -> bool:
        """Insert the row of one request; False when that request is already recorded."""
        return self.record_many((record,)) == 1

    def record_many(self, records: tuple[UsageRecord, ...]) -> int:
        """Insert these rows in one transaction; returns how many were new. A recorded request is skipped."""
        if type(records) is not tuple or any(type(record) is not UsageRecord for record in records):
            raise ProviderUsageError("invalid_usage_record")
        if not records:
            return 0
        now = self._clock().isoformat(timespec="microseconds")
        inserted = 0
        with self._connection(readonly=False) as connection:
            for record in records:
                cursor = connection.execute(
                    f"INSERT INTO provider_usage (project_id, {', '.join(_COLUMNS)}, recorded_at) "
                    f"VALUES (?, {', '.join('?' for _ in _COLUMNS)}, ?) ON CONFLICT (request_id) DO NOTHING",
                    (self._project_id, *(getattr(record, name) for name in _COLUMNS), now))
                inserted += cursor.rowcount
        return inserted

    def totals(self, job_ids: tuple[str, ...]) -> UsageTotals:
        """The sums over these jobs. A total with one unknown part is unknown."""
        if type(job_ids) is not tuple or not 1 <= len(job_ids) <= MAX_JOBS_PER_TOTAL:
            raise ProviderUsageError("invalid_job_ids")
        marks = ", ".join("?" for _ in job_ids)
        sums = ", ".join(f"CASE WHEN count({name}) = count(*) THEN coalesce(sum({name}), 0) END"
                         for name in ("input_tokens", "output_tokens", "cached_input_tokens", "cost_micro_usd"))
        with self._connection(readonly=True) as connection:
            row = connection.execute(
                f"SELECT count(*), coalesce(sum(characters), 0), coalesce(sum(words), 0), {sums} "
                f"FROM provider_usage WHERE project_id = ? AND job_id IN ({marks})",
                (self._project_id, *job_ids)).fetchone()
        return UsageTotals(*row)

    def records(self, job_id: str, *, limit: int = 10_000) -> tuple[UsageRecord, ...]:
        """The rows of one job, in the order they were recorded."""
        with self._connection(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM provider_usage WHERE project_id = ? AND job_id = ? "
                "ORDER BY recorded_at, request_id LIMIT ?", (self._project_id, job_id, limit)).fetchall()
        return tuple(UsageRecord(*row) for row in rows)
