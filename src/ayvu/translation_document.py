"""Translates one document with real project, job and unit identity.

Results are per unit, but persistence is per document. A single edit gives a document new
unit IDs anyway, so replay is document-wide in practice, and one artifact and one record per
unit would pass the project database's limit on a long book. The raw results of each unit
live inside the document's artifact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol
from uuid import uuid4

from .artifact import Artifact, ArtifactCancelledError, ArtifactError, ArtifactIntegrityError, ArtifactPathError, ArtifactRequest
from .artifact_service import ARTIFACT_SAFE_POINTS, ArtifactService
from .cache_identity import (
    MAX_CACHE_ONLY_CANDIDATES,
    AuthorizedCacheScope,
    CacheAuthorizationPolicy,
    CacheCompatibilityIdentity,
    CacheEligibility,
    CacheIdentityError,
    CacheIdentityResolutionState,
    CacheLookupState,
    CacheMissReason,
    CacheOnlyMode,
    CacheOnlyRequest,
    CacheProvenanceMetadata,
    CacheValueKind,
    CacheIdentityConflict,
    CompatibleMemoryCandidate,
    CompatibleMemoryQuery,
    ExactCacheEntry,
    ExactCacheLookup,
    ExactEntryKey,
    PinnedExecutionIdentityReference,
    PutIfAbsentResult,
    PutIfAbsentState,
    ResolvedCacheExecutionIdentity,
    TRANSLATION_STAGE_AGGREGATE,
    candidate_matches_query,
    exact_entry_key,
    resolve_compatible_identities,
)
from .content import ContentDocument, TranslationUnit
from .domain import LanguagePair
from .job import JobState, TranslationJob
from .job_events import (
    CancellationAcknowledgement, JobCorrelation, JobEvent, JobFailure, JobFailureFamily, JobProgress,
)
from .job_execution import CancellationSource, JobEventSink
from .provenance import (
    UNKNOWN, ActorKind, ConfigurationRef, ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef,
)
from .provenance_service import ProvenanceRepository
from .provider import ProviderStatus, TranslationProviderRequest, UnitReference
from .provider_service import TranslationProvider, execute
from .translation_memory import TranslationMemoryMatch
from .translation_pipeline import (
    GlossaryOutcome, LinguisticOutcome, LinguisticSource, LinguisticStages, ProtectedContentError,
)
from .translation_plan import ResolvedTranslationPlan
from .translation_protection import TAG_TOKEN_PATTERN, TextParts, protected_content_is_intact

RAW_RESULTS_VERSION = 1
RAW_RESULTS_ROLE = "raw_results"
RAW_RESULTS_STAGE = "raw_cache_write"
RAW_RESULTS_MEDIA_TYPE = "application/json"
PIPELINE_SAFE_POINTS = ("pipeline_before_unit", "pipeline_between_stages", "pipeline_between_chunks")
SAFE_POINTS = (*PIPELINE_SAFE_POINTS, *ARTIFACT_SAFE_POINTS)
_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


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
    PROVIDER = "provider"
    CACHE_ONLY_MISSING = "cache_only_missing"
    DRY_RUN = "dry_run"
    ARTIFACT_REPLAY = "artifact_replay"


_FROM_LINGUISTIC = {
    LinguisticSource.BLANK: UnitSource.BLANK,
    LinguisticSource.EXACT_CACHE: UnitSource.EXACT_CACHE,
    LinguisticSource.APPLIED_MEMORY: UnitSource.APPLIED_MEMORY,
    LinguisticSource.PROVIDER: UnitSource.PROVIDER,
    LinguisticSource.CACHE_ONLY_MISSING: UnitSource.CACHE_ONLY_MISSING,
    LinguisticSource.DRY_RUN: UnitSource.DRY_RUN,
}
_PROVENANCE_OPERATIONS = {
    UnitSource.EXACT_CACHE: ("reuse_exact_cache", SourceKind.CACHE, ActorKind.SYSTEM),
    UnitSource.APPLIED_MEMORY: ("apply_translation_memory", SourceKind.TRANSLATION_MEMORY, ActorKind.SYSTEM),
    UnitSource.PROVIDER: ("translate", SourceKind.PROVIDER, ActorKind.PROVIDER),
}


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

    def __repr__(self) -> str:
        return f"ExecutionContext(job_id={self.job.job_id!r})"


def _raw_payload(document: ContentDocument, plan: ResolvedTranslationPlan, raw: list[tuple[str, UnitSource, str | None]]) -> bytes:
    return json.dumps({
        "document_id": document.document_id,
        "raw_fingerprint": plan.raw_fingerprint,
        "revision_id": document.revision_id,
        "schema_version": RAW_RESULTS_VERSION,
        "units": [{"raw": text, "source": source.value, "unit_id": unit_id} for unit_id, source, text in raw],
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


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


class DocumentTranslator:
    def __init__(self, plan: ResolvedTranslationPlan, provider: TranslationProvider,
                 apply_glossary: Callable[[str], GlossaryOutcome], artifacts: ArtifactService,
                 provenance: ProvenanceRepository, events: JobEventSink, *, exact_cache: ExactCachePort | None = None,
                 memory: MemoryPort | None = None, clock: Callable[[], datetime],
                 new_id: Callable[[], str] = lambda: str(uuid4())) -> None:
        if type(plan) is not ResolvedTranslationPlan:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTERNAL, "invalid_plan"))
        self._plan = plan
        self._provider = provider
        self._apply_glossary = apply_glossary
        self._artifacts = artifacts
        self._provenance = provenance
        self._events = events
        self._exact_cache = exact_cache
        self._memory = memory
        self._clock = clock
        self._new_id = new_id

    def __repr__(self) -> str:
        return "DocumentTranslator()"

    def translate(self, document: ContentDocument, context: ExecutionContext, *, dry_run: bool = False,
                  cache_only: CacheOnlyRequest | None = None) -> DocumentTranslationResult:
        if cache_only is not None and type(cache_only) is not CacheOnlyRequest:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "cache_only_mode_required"))
        self._check_identity(document, context)
        scope = context.cache_scope
        if scope is None:
            scope = CacheAuthorizationPolicy(document.project_id).resolve()
        elif scope.project_id != document.project_id:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "cache_scope_denied"))
        resolved_pin = None
        if cache_only is not None and cache_only.mode is CacheOnlyMode.PINNED and self._exact_cache is not None:
            pin = cache_only.pin
            if pin is None:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "invalid_cache_only_request"))
            try:
                resolved_pin = self._exact_cache.resolve_pinned(pin, scope)
                if resolved_pin is not None and type(resolved_pin) is not ResolvedCacheExecutionIdentity:
                    raise CacheIdentityError("invalid_resolved_identity")
                if resolved_pin is not None and resolved_pin.scope != scope:
                    raise CacheIdentityError("cache_scope_denied")
            except CacheIdentityError as error:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, error.code)) from None
            except Exception:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INTERNAL, "cache_repository_failed")) from None
        run = _DocumentRun(self, document, context, scope, cache_only, resolved_pin)
        if cache_only is not None:
            # Cache-only is an exact cache operation; an artifact replay is another source.
            return self._execute(run, dry_run=dry_run, cache_only=cache_only)
        replayed = self._replay(run)
        if replayed is not None:
            return replayed
        return self._execute(run, dry_run=dry_run, cache_only=cache_only)

    def _check_identity(self, document: ContentDocument, context: ExecutionContext) -> None:
        def refuse(code: str) -> TranslationExecutionError:
            return TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, code))

        if type(document) is not ContentDocument or type(context) is not ExecutionContext:
            raise refuse("identity_required")
        job = context.job
        if type(job) is not TranslationJob or job.request.project_id != document.project_id:
            raise refuse("identity_mismatch")
        if job.state is not JobState.RUNNING:
            raise refuse("job_not_running")
        if job.lease is None or job.lease.owner_token != context.owner_token or job.lease.expired(self._clock()):
            raise refuse("lease_not_held")
        route = self._plan.route
        for unit in document.units:
            locale = unit.content.source_locale
            if locale.value is not None and locale.value != route.source:
                raise refuse("locale_conflicts_with_plan")
        if document.source_locale.value is not None and document.source_locale.value != route.source:
            raise refuse("locale_conflicts_with_plan")
        scope = context.cache_scope
        if scope is not None and (type(scope) is not AuthorizedCacheScope or scope.project_id != document.project_id):
            raise refuse("cache_scope_denied")

    def _artifact_request(self, document: ContentDocument, job: TranslationJob,
                          execution_nonce: str | None = None) -> ArtifactRequest:
        version = next(item.version for item in self._plan.stages if item.stage_id == RAW_RESULTS_STAGE)
        semantic_fingerprint = self._plan.semantic_fingerprint
        if semantic_fingerprint is None:
            # An unidentified producer cannot reuse even a job-local raw artifact.
            identity = json.dumps([document.document_id, document.revision_id, job.job_id, execution_nonce],
                                  separators=(",", ":"))
        else:
            identity = json.dumps([document.document_id, document.revision_id, semantic_fingerprint], separators=(",", ":"))
        return ArtifactRequest(document.project_id, job.job_id, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, version,
                               f"sha256:{hashlib.sha256(identity.encode('ascii')).hexdigest()}", RAW_RESULTS_MEDIA_TYPE)

    def _replay(self, run: _DocumentRun) -> DocumentTranslationResult | None:
        """Reuse raw results only when every identity and integrity check passes."""
        if self._plan.semantic_fingerprint is None:
            return None
        request = self._artifact_request(run.document, run.context.job)
        found = self._artifacts.find_reusable(request)
        if found.artifact is None:
            if found.reason != "artifact_not_found":
                # A damaged ready record would block new work forever, so withdraw it. Its file stays.
                self._artifacts.withdraw_corrupt(request)
            return None
        artifact = found.artifact
        try:
            with self._artifacts.open_reusable(artifact.artifact_id, request) as stream:
                payload = stream.read()
        except (ArtifactIntegrityError, ArtifactPathError):
            self._artifacts.retire_if_corrupt(artifact.artifact_id)
            return None
        raw = self._decode_raw(payload, run.document)
        results = []
        for index, unit in enumerate(run.document.units):
            run.checkpoint("pipeline_before_unit")
            results.append(run.apply_raw(unit, raw[index]))
            run.progress(index + 1)
        self._record_replay(run, artifact)
        return run.result(tuple(results), artifact.artifact_id)

    def _decode_raw(self, payload: bytes, document: ContentDocument) -> list[tuple[UnitSource, str | None]]:
        invalid = TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "invalid_raw_results"))
        try:
            data = json.loads(payload)
            if (type(data) is not dict or set(data) != {"document_id", "raw_fingerprint", "revision_id", "schema_version", "units"}
                    or data["schema_version"] != RAW_RESULTS_VERSION or data["document_id"] != document.document_id
                    or data["revision_id"] != document.revision_id or data["raw_fingerprint"] != self._plan.raw_fingerprint
                    or type(data["units"]) is not list or len(data["units"]) != len(document.units)):
                raise invalid
            decoded = []
            for unit, entry in zip(document.units, data["units"]):
                if type(entry) is not dict or set(entry) != {"raw", "source", "unit_id"} or entry["unit_id"] != unit.unit_id:
                    raise invalid
                source, raw = UnitSource(entry["source"]), entry["raw"]
                template, _ = _template(unit)
                core = TextParts.from_text(template).core
                if source is UnitSource.BLANK:
                    if raw is not None or core:
                        raise invalid
                elif (source not in _PROVENANCE_OPERATIONS or not isinstance(raw, str)
                        or not protected_content_is_intact(core, raw)):
                    raise invalid
                decoded.append((source, raw))
            return decoded
        except (ValueError, TypeError, RecursionError) as error:
            if isinstance(error, TranslationExecutionError):
                raise
            raise invalid from None

    def _execute(self, run: _DocumentRun, *, dry_run: bool,
                 cache_only: CacheOnlyRequest | None) -> DocumentTranslationResult:
        results = []
        raw: list[tuple[str, UnitSource, str | None]] = []
        complete = True
        for index, unit in enumerate(run.document.units):
            run.checkpoint("pipeline_before_unit")
            result, outcome = run.translate_unit(unit, dry_run=dry_run, cache_only=cache_only)
            results.append(result)
            raw.append((unit.unit_id, result.source, outcome.raw_text))
            complete = complete and result.source not in (UnitSource.CACHE_ONLY_MISSING, UnitSource.DRY_RUN)
            run.progress(index + 1)
        if not complete:
            return run.result(tuple(results), None)  # Nothing partial is ever published.
        artifact = run.publish(raw)
        self._record_production(run, artifact, raw)
        return run.result(tuple(results), artifact.artifact_id)

    def _references(self) -> tuple[ConfigurationRef, ...]:
        plan = self._plan
        pairs = (("context", plan.context_ref), ("glossary", plan.glossary_ref), ("policy", plan.policy_ref))
        return tuple(ConfigurationRef(kind, reference) for kind, reference in pairs if reference is not None)

    def _idempotency_key(self, run: _DocumentRun, operation: str, extra: str = "") -> str:
        identity = json.dumps([run.context.job.job_id, run.document.revision_id,
                               self._plan.semantic_fingerprint or run.execution_nonce, operation, extra],
                              separators=(",", ":"))
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    def _record_production(self, run: _DocumentRun, artifact: Artifact, raw: list) -> None:
        """One record per kind of origin in this document. Which unit came from where is in the artifact."""
        document, job = run.document, run.context.job
        subject = SubjectRef("content_revision", 1, document.revision_id)
        parents = []
        for source, (operation, source_kind, actor) in _PROVENANCE_OPERATIONS.items():
            if not any(item[1] is source for item in raw):
                continue
            producer = _UNKNOWN_PRODUCER if source is UnitSource.APPLIED_MEMORY else self._plan.producer
            parents.append(self._provenance.append(ProvenanceRequest(
                document.project_id, job.job_id, operation, actor, source_kind, producer, subject=subject,
                configuration_refs=self._references(),
                idempotency_key=self._idempotency_key(run, operation, self._plan.post_processing_fingerprint))).record_id)
        self._provenance.append(ProvenanceRequest(
            document.project_id, job.job_id, "store_raw_results", ActorKind.SYSTEM, SourceKind.PIPELINE, self._plan.producer,
            subject=subject, parents=tuple(parents), artifacts=(artifact.artifact_id,), configuration_refs=self._references(),
            # A redone document stores a new artifact, which is a new fact.
            idempotency_key=self._idempotency_key(run, "store_raw_results", artifact.artifact_id)))

    def _record_replay(self, run: _DocumentRun, artifact: Artifact) -> None:
        document, job = run.document, run.context.job
        # Only the record that stored it: earlier replays would make each identical retry look different.
        producers = [node for node in self._provenance.list_by_artifact(artifact.artifact_id, limit=16).nodes
                     if node.record.request.operation == "store_raw_results"]
        self._provenance.append(ProvenanceRequest(
            document.project_id, job.job_id, "replay_raw_results", ActorKind.SYSTEM, SourceKind.PIPELINE, self._plan.producer,
            subject=SubjectRef("content_revision", 1, document.revision_id),
            parents=tuple(node.record.record_id for node in producers), artifacts=(artifact.artifact_id,),
            configuration_refs=self._references(),
            idempotency_key=self._idempotency_key(run, "replay_raw_results",
                                                  f"{artifact.artifact_id}:{self._plan.post_processing_fingerprint}")))


class _ContractProvider:
    """Presents the rich provider as text calls to the stages, checking each result against the plan."""

    def __init__(self, translator: DocumentTranslator, run: _DocumentRun, unit: TranslationUnit) -> None:
        self._translator = translator
        self._run = run
        self._unit = unit

    def translate(self, text: str, source: str, target: str) -> str:
        self._run.checkpoint("pipeline_between_chunks")
        plan = self._translator._plan
        document = self._run.document
        request = TranslationProviderRequest(
            self._translator._new_id(), text, source, target, route=plan.route,
            unit=UnitReference(document.document_id, document.revision_id, self._unit.unit_id),
            policy_ref=plan.policy_ref)
        result = execute(self._translator._provider, request)
        for warning in result.warnings:
            self._run.emit(warning)
        if result.status is not ProviderStatus.SUCCEEDED:
            raise _ProviderStopped(result.failure)
        if result.producer != plan.producer or result.route != plan.route:
            # What really ran differs from what was resolved, so a new plan is needed.
            raise _ProviderStopped(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "plan_invalidated"))
        return result.text


class _PlanBoundCache:
    def __init__(self, cache: ExactCachePort | None, plan: ResolvedTranslationPlan, scope: AuthorizedCacheScope,
                 cache_only: CacheOnlyRequest | None, resolved_pin: ResolvedCacheExecutionIdentity | None,
                 source_text: str) -> None:
        self._cache = cache
        self._plan = plan
        self._scope = scope
        self._cache_only = cache_only
        self._resolved_pin = resolved_pin
        self._source_text = source_text
        self._fingerprint = plan.semantic_fingerprint
        self.eligibility = plan.cache_eligibility
        self._value_kind = TRANSLATION_STAGE_AGGREGATE

    def _miss(self, reason: CacheMissReason) -> ExactCacheLookup:
        return ExactCacheLookup(CacheLookupState.MISS, reason=reason)

    def get(self, text: str, language_pair: LanguagePair) -> ExactCacheLookup:
        from .cache_identity import CACHE_ENTRY_V2_LIMITS
        if len(self._source_text.encode("utf-8")) > CACHE_ENTRY_V2_LIMITS.source_text:
            return self._miss(CacheMissReason.CACHE_INELIGIBLE)
        if self._fingerprint is None:
            return self._miss(CacheMissReason.CACHE_INELIGIBLE)
        if self._cache is None:
            return self._miss(CacheMissReason.ENTRY_NOT_FOUND)
        key = exact_entry_key(self._scope, self._value_kind, self._fingerprint, self._source_text)
        if self._cache_only is not None and self._cache_only.mode is CacheOnlyMode.PINNED:
            pin = self._resolved_pin
            if pin is None:
                return self._miss(CacheMissReason.ENTRY_NOT_FOUND)
            if pin.value_kind != self._value_kind:
                return self._miss(CacheMissReason.IDENTITY_MISMATCH)
            if pin.semantic_fingerprint != self._fingerprint:
                return self._miss(CacheMissReason.IDENTITY_MISMATCH)
            key = exact_entry_key(pin.scope, pin.value_kind, pin.semantic_fingerprint, self._source_text)
        elif self._cache_only is not None and self._cache_only.mode is CacheOnlyMode.RESOLVE_COMPATIBLE:
            try:
                candidates = self._cache.resolve_compatible(
                    self._scope, self._value_kind, key.source_content_digest, limit=MAX_CACHE_ONLY_CANDIDATES,
                )
            except CacheIdentityError:
                raise
            except Exception:
                raise CacheIdentityError("cache_repository_failed") from None
            if type(candidates) is not tuple:
                raise CacheIdentityError("invalid_identity_resolution")
            resolution = resolve_compatible_identities(
                candidates, scope=self._scope, value_kind=self._value_kind,
                source_digest=key.source_content_digest,
            )
            if resolution.state is CacheIdentityResolutionState.MISS:
                return self._miss(CacheMissReason.ENTRY_NOT_FOUND)
            if resolution.state is CacheIdentityResolutionState.AMBIGUOUS:
                return ExactCacheLookup(CacheLookupState.AMBIGUOUS, reason=CacheMissReason.AMBIGUOUS_CANDIDATES)
            if resolution.state is CacheIdentityResolutionState.CORRUPT:
                raise CacheIdentityError("duplicate_exact_key")
            if resolution.key is None:
                raise CacheIdentityError("invalid_identity_resolution")
            if resolution.key.semantic_fingerprint != self._fingerprint:
                return self._miss(CacheMissReason.IDENTITY_MISMATCH)
            key = resolution.key
        try:
            result = self._cache.lookup(key)
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("cache_repository_failed") from None
        if type(result) is not ExactCacheLookup:
            raise CacheIdentityError("invalid_cache_lookup")
        if result.state is CacheLookupState.CORRUPT:
            raise CacheIdentityError("duplicate_exact_key")
        return result

    def set(self, text: str, language_pair: LanguagePair, translated: str) -> None:
        from .cache_identity import CACHE_ENTRY_V2_LIMITS
        if (len(self._source_text.encode("utf-8")) > CACHE_ENTRY_V2_LIMITS.source_text
                or len(translated.encode("utf-8")) > CACHE_ENTRY_V2_LIMITS.value):
            return
        if self._cache is None or self._fingerprint is None or self._cache_only is not None:
            return
        key = exact_entry_key(self._scope, self._value_kind, self._fingerprint, self._source_text)
        operation = self._plan.semantic_operation
        if operation is None:
            return
        try:
            result = self._cache.put_if_absent(ExactCacheEntry(
                key, translated, CacheProvenanceMetadata("translate", self._plan.producer, operation.pipeline_revision),
                source_text=self._source_text, canonical_identity=operation.canonical_payload(),
            ))
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("cache_repository_failed") from None
        if type(result) is not PutIfAbsentResult:
            raise CacheIdentityError("invalid_put_result")
        if result.state is PutIfAbsentState.CONFLICT:
            raise CacheIdentityConflict("cache_write_conflict")


class _PlanBoundMemory:
    def __init__(self, memory: MemoryPort, plan: ResolvedTranslationPlan, scope: AuthorizedCacheScope) -> None:
        self._memory = memory
        self._plan = plan
        self._scope = scope

    def lookup(self, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None:
        fingerprint = self._plan.semantic_fingerprint
        if fingerprint is None:
            return None
        query = CompatibleMemoryQuery(
            self._scope, TRANSLATION_STAGE_AGGREGATE, fingerprint, text, MAX_CACHE_ONLY_CANDIDATES,
        )
        try:
            candidate = self._memory.lookup(query)
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("memory_repository_failed") from None
        if candidate is None or not candidate_matches_query(candidate, query):
            return None
        return TranslationMemoryMatch(candidate.original, candidate.translated, candidate.score, candidate.applied)


class _DocumentRun:
    def __init__(self, translator: DocumentTranslator, document: ContentDocument, context: ExecutionContext,
                 cache_scope: AuthorizedCacheScope, cache_only: CacheOnlyRequest | None,
                 resolved_pin: ResolvedCacheExecutionIdentity | None) -> None:
        self.translator = translator
        self.document = document
        self.context = context
        self.cache_scope = cache_scope
        self.cache_only = cache_only
        self.resolved_pin = resolved_pin
        self.execution_nonce = str(uuid4())
        self.sequence = context.first_sequence
        self.correlation = JobCorrelation(document.project_id)

    def emit(self, payload: object) -> None:
        translator = self.translator
        translator._events.emit(JobEvent(translator._new_id(), self.context.job.job_id, self.sequence, translator._clock(),
                                         self.correlation, payload))
        self.sequence += 1

    def progress(self, completed: int) -> None:
        self.emit(JobProgress(self.context.progress_phase, "units", completed, len(self.document.units)))

    def checkpoint(self, safe_point: str) -> None:
        source = self.context.cancellation
        if source is None:
            return
        acknowledgement = source.token.checkpoint(safe_point)
        if acknowledgement is not None:
            self._stop(acknowledgement)

    def _stop(self, acknowledgement: CancellationAcknowledgement) -> None:
        self.emit(self.context.cancellation.requested)
        self.emit(acknowledgement)
        raise TranslationCancelled()

    def _stage_entered(self, stage_id: str) -> None:
        if stage_id != "unit_whitespace":
            self.checkpoint("pipeline_between_stages")

    def translate_unit(self, unit: TranslationUnit, *, dry_run: bool,
                       cache_only: CacheOnlyRequest | None) -> tuple[UnitTranslationResult, LinguisticOutcome]:
        translator = self.translator
        plan = translator._plan
        template, originals = _template(unit)
        stages = LinguisticStages(
            _ContractProvider(translator, self, unit),
            _PlanBoundCache(translator._exact_cache, plan, self.cache_scope, self.cache_only, self.resolved_pin,
                            unit.source_text),
            translator._apply_glossary,
            memory=None if translator._memory is None else _PlanBoundMemory(translator._memory, plan, self.cache_scope),
            on_stage=self._stage_entered)
        try:
            outcome = stages.run(template, LanguagePair(source=plan.route.source, target=plan.route.target),
                                 dry_run=dry_run, cache_only=self.cache_only or False, chunk_limit=plan.chunk_limit)
        except _ProviderStopped as stopped:
            raise TranslationExecutionError(stopped.failure) from None
        except ProtectedContentError:
            # Stage messages are for the current CLI; this path reports a code and no text.
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "protected_content_not_preserved")) from None
        except CacheIdentityError as error:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, error.code)) from None
        source = _FROM_LINGUISTIC[outcome.source]
        # An untranslated unit expands back to its exact source text.
        return UnitTranslationResult(unit.unit_id, source, _expand(outcome.text, originals), outcome.memory_suggestion is not None,
                                     outcome.glossary_usage, outcome.text, outcome.cache_lookup_state,
                                     outcome.cache_miss_reason), outcome

    def apply_raw(self, unit: TranslationUnit, entry: tuple[UnitSource, str | None]) -> UnitTranslationResult:
        """Only post-processing runs again: no cache lookup and no provider."""
        source, raw = entry
        if source is UnitSource.BLANK:
            return UnitTranslationResult(unit.unit_id, UnitSource.BLANK, unit.source_text, tokenized_text=_template(unit)[0])
        template, originals = _template(unit)
        parts = TextParts.from_text(template)
        application = self.translator._apply_glossary(raw)
        tokenized = parts.restore(application.text)
        return UnitTranslationResult(unit.unit_id, UnitSource.ARTIFACT_REPLAY, _expand(tokenized, originals),
                                     False, application.usage, tokenized)

    def publish(self, raw: list[tuple[str, UnitSource, str | None]]) -> Artifact:
        translator = self.translator
        source = self.context.cancellation
        try:
            return translator._artifacts.publish(
                translator._artifact_request(self.document, self.context.job, self.execution_nonce),
                (_raw_payload(self.document, translator._plan, raw),),
                owner_token=self.context.owner_token, cancellation=None if source is None else source.token)
        except ArtifactCancelledError:
            self._stop(source.acknowledgement)
        except ArtifactError as error:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTERNAL, str(error))) from None

    def result(self, units: tuple[UnitTranslationResult, ...], artifact_id: str | None) -> DocumentTranslationResult:
        return DocumentTranslationResult(self.document.document_id, self.document.revision_id, units, artifact_id, self.sequence,
                                         self.translator._plan.cache_eligibility)
