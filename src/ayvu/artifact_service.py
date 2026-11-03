"""Application rules for publishing, reusing and reconciling job artifacts.

SQLite and the filesystem cannot commit together. The protocol therefore orders
the steps so that every interruption leaves a state reconciliation can resolve,
and a record says `ready` only after the final file was verified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable, Protocol

from .artifact import (
    Artifact, ArtifactCancelledError, ArtifactConflictError, ArtifactError, ArtifactIntegrityError,
    ArtifactNotFoundError, ArtifactPathError, ArtifactRequest, ArtifactState, ArtifactStoreError, reuse_mismatch,
)
from .job_execution import CancellationToken

ARTIFACT_SAFE_POINTS = ('artifact_before_write', 'artifact_between_chunks', 'artifact_before_publication')
MAX_RECONCILE_BATCH = 1024


@dataclass(frozen=True)
class StagedArtifact:
    artifact: Artifact
    job_active: bool


@dataclass(frozen=True)
class PayloadEvidence:
    """``final`` is one of ``absent``, ``match`` and ``mismatch``."""

    final: str


class ArtifactRepository(Protocol):
    """Every operation is one short transaction; none performs payload I/O."""

    def reserve(self, request: ArtifactRequest, owner_token: str) -> Artifact: ...
    def record_payload(self, artifact_id: str, expected_revision: int, size: int, digest: str,
                       owner_token: str) -> Artifact: ...
    def mark_ready(self, artifact_id: str, expected_revision: int, owner_token: str) -> Artifact: ...
    def abandon(self, artifact_id: str, expected_revision: int, reason_code: str, owner_token: str) -> Artifact: ...
    def get(self, artifact_id: str) -> Artifact: ...
    def find_ready(self, request: ArtifactRequest) -> Artifact | None: ...
    def list_staging(self, limit: int) -> tuple[StagedArtifact, ...]: ...
    def settle_ready(self, artifact_id: str, expected_revision: int) -> Artifact: ...
    def settle_abandoned(self, artifact_id: str, expected_revision: int, reason_code: str) -> Artifact: ...
    def retire(self, artifact_id: str, expected_revision: int, reason_code: str) -> Artifact: ...


class ArtifactPayloadStore(Protocol):
    def write_staged(self, artifact_id: str, chunks: Iterable[bytes],
                     between_chunks: Callable[[], None]) -> tuple[int, str]: ...
    def publish(self, artifact_id: str, size: int, digest: str) -> None: ...
    def evidence(self, artifact_id: str, size: int | None, digest: str | None) -> PayloadEvidence: ...
    def complete_publication(self, artifact_id: str) -> None: ...
    def open_verified(self, artifact_id: str, size: int, digest: str) -> BinaryIO: ...


@dataclass(frozen=True)
class ReuseResult:
    """``reason`` is None only when ``artifact`` passed every check, including its digest."""

    artifact: Artifact | None
    reason: str | None


@dataclass(frozen=True)
class ReconciliationReport:
    completed: tuple[str, ...] = ()
    abandoned: tuple[str, ...] = ()
    active: tuple[str, ...] = ()
    more: bool = False


class ArtifactService:
    def __init__(self, repository: ArtifactRepository, payloads: ArtifactPayloadStore) -> None:
        self._repository = repository
        self._payloads = payloads

    def __repr__(self) -> str:
        return "ArtifactService()"

    def publish(self, request: ArtifactRequest, chunks: Iterable[bytes], *, owner_token: str,
                cancellation: CancellationToken | None = None) -> Artifact:
        def checkpoint(safe_point: str) -> None:
            if cancellation is not None and cancellation.checkpoint(safe_point) is not None:
                raise ArtifactCancelledError("publication_cancelled")

        checkpoint('artifact_before_write')
        artifact = self._repository.reserve(request, owner_token)
        artifact_id = artifact.artifact_id
        try:
            size, digest = self._payloads.write_staged(
                artifact_id, chunks, lambda: checkpoint('artifact_between_chunks'))
            artifact = self._repository.record_payload(artifact_id, artifact.revision, size, digest, owner_token)
            checkpoint('artifact_before_publication')
        except ArtifactCancelledError:
            self._abandon_quietly(artifact, 'cancelled', owner_token)
            raise
        except ArtifactError as error:
            self._abandon_quietly(artifact, str(error), owner_token)
            raise
        except Exception:
            # The chunks come from the caller; their exception may quote the document.
            self._abandon_quietly(artifact, 'payload_source_failed', owner_token)
            raise ArtifactStoreError('payload_source_failed') from None
        # From here the file may already be visible, so a failure leaves the record
        # staged: only reconciliation can tell a complete publication from a partial one.
        self._payloads.publish(artifact_id, size, digest)
        return self._repository.mark_ready(artifact_id, artifact.revision, owner_token)

    def _abandon_quietly(self, artifact: Artifact, reason_code: str, owner_token: str) -> None:
        try:
            self._repository.abandon(artifact.artifact_id, artifact.revision, reason_code, owner_token)
        except ArtifactError:
            # An expired lease or an unavailable store: the record stays staged,
            # never ready, and reconciliation resolves it later.
            pass

    def find_reusable(self, request: ArtifactRequest) -> ReuseResult:
        """Read-only. A record that fails verification is reported, not changed."""
        artifact = self._repository.find_ready(request)
        if artifact is None:
            return ReuseResult(None, 'artifact_not_found')
        reason = reuse_mismatch(artifact, request)
        if reason is not None:
            return ReuseResult(None, reason)
        try:
            self._payloads.open_verified(artifact.artifact_id, artifact.size, artifact.digest).close()
        except (ArtifactIntegrityError, ArtifactPathError) as error:
            return ReuseResult(None, str(error))
        return ReuseResult(artifact, None)

    def open_reusable(self, artifact_id: str, request: ArtifactRequest) -> BinaryIO:
        """A verified open handle, so the checked bytes are the bytes the caller reads."""
        artifact = self._repository.get(artifact_id)
        reason = reuse_mismatch(artifact, request)
        if reason is not None:
            raise ArtifactConflictError(reason)
        return self._payloads.open_verified(artifact_id, artifact.size, artifact.digest)

    def retire_if_corrupt(self, artifact_id: str) -> Artifact:
        """Withdraw a ready record whose payload no longer matches, so the work can be redone."""
        artifact = self._repository.get(artifact_id)
        if artifact.state is not ArtifactState.READY:
            return artifact
        evidence = self._safe_evidence(artifact)
        if evidence == 'match':
            return artifact
        return self._repository.retire(artifact_id, artifact.revision, 'integrity_failed')

    def withdraw_corrupt(self, request: ArtifactRequest) -> Artifact | None:
        """The same check for a caller that holds a request and not an artifact ID."""
        artifact = self._repository.find_ready(request)
        return None if artifact is None else self.retire_if_corrupt(artifact.artifact_id)

    def _safe_evidence(self, artifact: Artifact) -> str:
        try:
            return self._payloads.evidence(artifact.artifact_id, artifact.size, artifact.digest).final
        except ArtifactPathError:
            return 'mismatch'

    def reconcile(self, *, limit: int = MAX_RECONCILE_BATCH) -> ReconciliationReport:
        """Resolve staged records with no live owner. Payload files are never deleted."""
        staged = self._repository.list_staging(limit + 1)
        completed: list[str] = []
        abandoned: list[str] = []
        active: list[str] = []
        for entry in staged[:limit]:
            artifact_id = entry.artifact.artifact_id
            if entry.job_active:
                active.append(artifact_id)
                continue
            try:
                outcome = self._reconcile_one(entry.artifact)
            except (ArtifactConflictError, ArtifactNotFoundError):
                # A new owner or another reconciler got there first; it is no longer ours.
                active.append(artifact_id)
                continue
            (completed if outcome.state is ArtifactState.READY else abandoned).append(artifact_id)
        return ReconciliationReport(tuple(completed), tuple(abandoned), tuple(active), len(staged) > limit)

    def _reconcile_one(self, artifact: Artifact) -> Artifact:
        artifact_id, revision = artifact.artifact_id, artifact.revision
        if artifact.size is None:
            return self._repository.settle_abandoned(artifact_id, revision, 'interrupted_before_digest')
        try:
            final = self._payloads.evidence(artifact_id, artifact.size, artifact.digest).final
        except ArtifactPathError:
            return self._repository.settle_abandoned(artifact_id, revision, 'unsafe_location')
        if final == 'absent':
            return self._repository.settle_abandoned(artifact_id, revision, 'interrupted_before_publication')
        if final != 'match':
            return self._repository.settle_abandoned(artifact_id, revision, 'final_mismatch')
        self._payloads.complete_publication(artifact_id)
        return self._repository.settle_ready(artifact_id, revision)
