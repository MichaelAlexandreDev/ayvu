"""The project's consent to remote egress (#379): asked once, recorded, checked before any remote request.

The gate exists only while a project-backed translation prepares its model, so a run without a project
(a preview, a dry run, a legacy resume) has no gate and a remote preflight refuses it. The owner's
answer comes from the interface's callback: the guided and common modes ask, developer mode answers
with its explicit flag. Nothing but the provider, the UTC time and the text version is recorded.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from ...projects import ProjectError
from ...projects.consent import REMOTE_EGRESS_TEXT_VERSION
from ...projects.schema import open_remote_consents
from ...projects.store import ProjectJsonRepository, source_digest
from ...translation import PreflightError


@dataclass(frozen=True)
class RemoteEgressNotice:
    """What the owner agrees to: this provider, these hosts, this retention claim, under this text version."""

    provider: str
    hosts: tuple[str, ...]
    retention_claim: str
    text_version: str = REMOTE_EGRESS_TEXT_VERSION


@dataclass(frozen=True)
class RevocationResult:
    projects: int
    removed: int


def _unavailable() -> PreflightError:
    return PreflightError("The project's remote consent could not be read or recorded.",
                          "Check the permissions of the processing folder and try again. Nothing was sent.")


class ConsentGate:
    """One project's consent, asked through ``ask`` only when no consent to the current text is recorded."""

    def __init__(self, processing_dir: Path, project_id: str, ask: Callable[[RemoteEgressNotice], bool] | None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._processing_dir, self._project_id, self._ask, self._clock = processing_dir, project_id, ask, clock

    def __repr__(self) -> str:
        return "ConsentGate()"

    def require(self, notice: RemoteEgressNotice) -> None:
        try:
            paths = ProjectJsonRepository(self._processing_dir).resolve_database_paths(self._project_id)
            consents = open_remote_consents(paths.database, paths.backups, self._project_id, clock=self._clock)
            if consents.current(notice.provider, notice.text_version) is not None:
                return
        except (ProjectError, OSError):
            raise _unavailable() from None
        if self._ask is None or self._ask(notice) is not True:
            raise PreflightError("Remote translation needs your consent. Nothing was sent.",
                                 "Answer yes when Ayvu asks, or, in developer mode, run with --consent-remote.",
                                 detail="remote_consent_refused")
        try:
            consents.grant(notice.provider, notice.text_version)
        except (ProjectError, OSError):
            raise _unavailable() from None


_GATE: ContextVar[ConsentGate | None] = ContextVar("remote_consent_gate", default=None)


@contextmanager
def consent_scope(gate: ConsentGate) -> Iterator[None]:
    token = _GATE.set(gate)
    try:
        yield
    finally:
        _GATE.reset(token)


def current_gate() -> ConsentGate | None:
    return _GATE.get()


def project_gate(book: object) -> ConsentGate | None:
    """The gate of a project-backed book, or None when the book has no project."""
    processing_dir, project_id = getattr(book, "processing_dir", None), getattr(book, "project_id", None)
    if processing_dir is None or project_id is None:
        return None
    return ConsentGate(processing_dir, project_id, getattr(book, "remote_consent", None))


def revoke_remote_consent(processing_dir: Path, target: str, translator: str | None = None) -> RevocationResult:
    """Remove the recorded consent of one project (by id) or of every project of one EPUB (by path).

    Raises ``ProjectError`` for an unknown project or an unreadable store; an EPUB without projects
    revokes nothing.
    """
    repository = ProjectJsonRepository(processing_dir)
    path = Path(target).expanduser()
    if path.is_file():
        _size, digest = source_digest(path)
        project_ids = tuple(item.project_id for item in repository.list_projects() if item.source_sha256 == digest)
    else:
        project_ids = (repository.load_read_only(target).project_id,)
    removed = 0
    for project_id in project_ids:
        paths = repository.resolve_database_paths(project_id)
        if paths.database.exists():
            removed += open_remote_consents(paths.database, paths.backups, project_id).revoke(translator)
    return RevocationResult(len(project_ids), removed)
