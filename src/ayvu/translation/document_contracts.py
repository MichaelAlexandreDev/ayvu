"""Contracts, safe points and protected content for a document translation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..artifacts import ARTIFACT_SAFE_POINTS
from ..knowledge.cache import (
    AuthorizedCacheScope, CacheEligibility, CacheLookupState, CacheMissReason, CacheValueKind,
    CompatibleMemoryCandidate, CompatibleMemoryQuery, ExactCacheEntry, ExactCacheLookup, ExactEntryKey,
    PinnedExecutionIdentityReference, PutIfAbsentResult, ResolvedCacheExecutionIdentity,
)
from ..knowledge.memory.approved import ValueKind, PipelineStage
from .stages import MemoryMatch, RetrievalDecision
from ..content import TranslationUnit
from ..jobs import TranslationJob
from ..jobs import CancellationSource
from ..content import TAG_TOKEN_PATTERN
from ..kernel import JobFailure, JobFailureFamily


# How far back a continuation looks for raw results. A chain this long is already unusual.
MAX_REUSABLE_JOBS = 32
PIPELINE_SAFE_POINTS = ("pipeline_before_unit", "pipeline_between_stages", "pipeline_between_chunks")
SAFE_POINTS = (*PIPELINE_SAFE_POINTS, *ARTIFACT_SAFE_POINTS)


class TranslationExecutionError(ValueError):
    """Carries a sanitized failure. Nothing was published as complete."""

    def __init__(self, failure: JobFailure) -> None:
        self.failure = failure
        super().__init__(failure.code)


class TranslationCancelled(TranslationExecutionError):
    def __init__(self) -> None:
        super().__init__(JobFailure(JobFailureFamily.CANCELLED, "translation_cancelled"))


class _ProviderStopped(Exception):
    def __init__(self, failure: JobFailure) -> None:
        self.failure = failure
        super().__init__(failure.code)


class UsageRecorder(Protocol):
    """Where each provider result's usage goes (``translation.usage.RunMeter``). It holds counts, never text."""

    def record(self, *, job_id: str, document_id: str, unit_id: str, request_id: str, text: str,
               usage: object, delivery: object) -> None: ...


class ExactCachePort(Protocol):
    """V2 local repository; identity resolution and cache-only operations must never use transport."""

    def resolve_pinned(self, reference: PinnedExecutionIdentityReference,
                       scope: AuthorizedCacheScope) -> ResolvedCacheExecutionIdentity | None: ...
    def lookup(self, key: ExactEntryKey) -> ExactCacheLookup: ...
    def resolve_compatible(self, scope: AuthorizedCacheScope, value_kind: CacheValueKind,
                           source_digest: str, *, limit: int) -> tuple[ExactEntryKey, ...]: ...
    def put_if_absent(self, entry: ExactCacheEntry) -> PutIfAbsentResult: ...


class MemoryPort(Protocol):
    """Returns local candidates in the authorized compatible namespace; v1 memory cannot satisfy it."""

    def lookup(self, query: CompatibleMemoryQuery) -> CompatibleMemoryCandidate | None: ...


class UnitSource(StrEnum):
    BLANK = "blank"
    EXACT_CACHE = "exact_cache"
    APPLIED_MEMORY = "applied_memory"
    APPROVED_MEMORY = "approved_memory"
    PROVIDER = "provider"
    CACHE_ONLY_MISSING = "cache_only_missing"
    DRY_RUN = "dry_run"
    ARTIFACT_REPLAY = "artifact_replay"


@dataclass(frozen=True, repr=False)
class UnitTranslationResult:
    unit_id: str
    source: UnitSource
    text: str
    memory_suggested: bool = False
    glossary_usage: object | None = None
    # ``text`` with tokens instead of the markup they stand for. Only this form tells original
    # markup from whatever a provider wrote, so reconstruction must use this one.
    tokenized_text: str | None = None
    cache_lookup_state: CacheLookupState | None = None
    cache_miss_reason: CacheMissReason | None = None
    retrieval_decision: RetrievalDecision | None = None
    retrieval_suggestions: tuple[MemoryMatch, ...] = ()
    value_kind: ValueKind | None = None
    value_stage: PipelineStage | None = None

    def __repr__(self) -> str:
        return f"UnitTranslationResult(unit_id={self.unit_id!r}, source={self.source.value!r})"


@dataclass(frozen=True, repr=False)
class DocumentTranslationResult:
    document_id: str
    revision_id: str
    units: tuple[UnitTranslationResult, ...]
    artifact_id: str | None
    next_sequence: int
    cache_eligibility: CacheEligibility | None = None

    def __repr__(self) -> str:
        return f"DocumentTranslationResult(document_id={self.document_id!r}, units={len(self.units)})"

    @property
    def complete(self) -> bool:
        return not any(item.source in (UnitSource.CACHE_ONLY_MISSING, UnitSource.DRY_RUN) for item in self.units)


@dataclass(frozen=True, repr=False)
class ExecutionContext:
    """Real identity, required: there is no way to run this path without a project, job and owner."""

    job: TranslationJob
    owner_token: str
    cancellation: CancellationSource | None = None
    first_sequence: int = 1
    # A phase cannot return, so a job translating several documents gives each its own.
    progress_phase: str = "translate"
    cache_scope: AuthorizedCacheScope | None = None
    # Earlier jobs of the same continuation chain, nearest first, whose raw results this job may prove and reuse.
    reusable_jobs: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"ExecutionContext(job_id={self.job.job_id!r})"


def _template(unit: TranslationUnit) -> tuple[str, list[str]]:
    """Protected spans become tag tokens, which the stages keep in order or refuse."""
    text = unit.source_text
    pieces: list[str] = []
    originals: list[str] = []
    cursor = 0
    plain: list[str] = []
    for part in unit.content.protected_parts:
        plain.append(text[cursor:part.start])
        pieces.append(plain[-1])
        pieces.append(f"__AYVU_TAG_{len(originals)}__")
        originals.append(text[part.start:part.end])
        cursor = part.end
    plain.append(text[cursor:])
    pieces.append(plain[-1])
    # Inside a protected part such text is safe: it is never read as a token.
    if any(TAG_TOKEN_PATTERN.search(piece) for piece in plain):
        raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "reserved_placeholder_in_source"))
    return "".join(pieces), originals


def _expand(text: str, originals: list[str]) -> str:
    return TAG_TOKEN_PATTERN.sub(lambda match: originals[int(match.group(1))], text)
