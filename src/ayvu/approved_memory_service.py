"""Application port and explicit governed-memory commands; no pipeline bridge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .approved_memory import (
    Candidate, MemoryEvent, MemoryPage, MemoryRevision, MemoryScope, MemorySource, MemoryState, TargetValue,
)
from .domain import LanguagePair


@dataclass(frozen=True, repr=False)
class CreateCandidate:
    operation_id: str
    value: Candidate


@dataclass(frozen=True, repr=False)
class DeriveRevision:
    operation_id: str
    parent_revision: str
    expected_version: int
    value: Candidate
    human_edit: bool = False


@dataclass(frozen=True, repr=False)
class ReviewRevision:
    operation_id: str
    revision_id: str
    expected_version: int
    scope: MemoryScope
    provenance_id: str
    state: MemoryState
    reviewed_value: TargetValue | None = None
    expected_approval: str | None = None


class ApprovedMemoryRepository(Protocol):
    """Every query explicitly names its scopes, in caller-selected precedence.

    Writes are atomic and operation IDs identify exact commands, including their
    expected versions. Replays return the original snapshot, even after later
    transitions. Derivations fork history without modifying the parent value.
    """

    def create(self, command: CreateCandidate) -> MemoryRevision: ...
    def derive(self, command: DeriveRevision) -> MemoryRevision: ...
    def review(self, command: ReviewRevision) -> MemoryRevision: ...
    def get(self, revision_id: str, *, scopes: tuple[MemoryScope, ...]) -> MemoryRevision: ...
    def list_entries(self, *, scopes: tuple[MemoryScope, ...], limit: int = 100, after: int = 0,
                     state: MemoryState | None = None, entry_id: str | None = None) -> MemoryPage: ...
    def history(self, revision_id: str, *, scopes: tuple[MemoryScope, ...],
                limit: int = 100, after: int = 0) -> tuple[MemoryEvent, ...]: ...
    def current(self, source: MemorySource, languages: LanguagePair, *,
                scopes: tuple[MemoryScope, ...]) -> MemoryRevision | None: ...


class ApprovedMemory:
    """Review callers provide human provenance, exact value and optimistic versions."""

    def __init__(self, repository: ApprovedMemoryRepository) -> None:
        self.repository = repository

    def create_candidate(self, command: CreateCandidate) -> MemoryRevision:
        return self.repository.create(command)

    def create_revision(self, command: DeriveRevision) -> MemoryRevision:
        return self.repository.derive(command)

    def record_human_edit(self, *, operation_id: str, parent_revision: str, expected_version: int,
                          value: Candidate) -> MemoryRevision:
        return self.repository.derive(DeriveRevision(operation_id, parent_revision, expected_version, value, True))

    def approve(self, *, operation_id: str, revision_id: str, expected_version: int, scope: MemoryScope,
                provenance_id: str, reviewed_value: TargetValue, expected_approval: str | None) -> MemoryRevision:
        return self.repository.review(ReviewRevision(operation_id, revision_id, expected_version, scope,
                                                     provenance_id, MemoryState.APPROVED, reviewed_value,
                                                     expected_approval))

    def reject(self, *, operation_id: str, revision_id: str, expected_version: int, scope: MemoryScope,
               provenance_id: str) -> MemoryRevision:
        return self.repository.review(ReviewRevision(operation_id, revision_id, expected_version, scope,
                                                     provenance_id, MemoryState.REJECTED))

    def deprecate(self, *, operation_id: str, revision_id: str, expected_version: int, scope: MemoryScope,
                  provenance_id: str) -> MemoryRevision:
        return self.repository.review(ReviewRevision(operation_id, revision_id, expected_version, scope,
                                                     provenance_id, MemoryState.DEPRECATED))

    def get(self, revision_id: str, *, scopes: tuple[MemoryScope, ...]) -> MemoryRevision:
        return self.repository.get(revision_id, scopes=scopes)

    def list_entries(self, *, scopes: tuple[MemoryScope, ...], limit: int = 100, after: int = 0,
                     state: MemoryState | None = None, entry_id: str | None = None) -> MemoryPage:
        return self.repository.list_entries(scopes=scopes, limit=limit, after=after, state=state, entry_id=entry_id)

    def current(self, source: MemorySource, languages: LanguagePair, *,
                scopes: tuple[MemoryScope, ...]) -> MemoryRevision | None:
        return self.repository.current(source, languages, scopes=scopes)

    def history(self, revision_id: str, *, scopes: tuple[MemoryScope, ...],
                limit: int = 100, after: int = 0) -> tuple[MemoryEvent, ...]:
        return self.repository.history(revision_id, scopes=scopes, limit=limit, after=after)
