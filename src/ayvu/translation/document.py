"""Translates one document with real project, job and unit identity.

Results are per unit, but persistence is per document. A single edit gives a document new
unit IDs anyway, so replay is document-wide in practice, and one artifact and one record per
unit would pass the project database's limit on a long book. The raw results of each unit
live inside the document's artifact.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol
from uuid import uuid4

from ..artifacts import Artifact, ArtifactCancelledError, ArtifactError, ArtifactIntegrityError, ArtifactPathError, ArtifactRequest
from ..artifacts import ARTIFACT_SAFE_POINTS, ArtifactService
from ..knowledge.cache import (
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
from ..knowledge.memory.approved import MemoryError, MemoryQuery, MemoryScope, ScopeKind, RetrievalMode, ValueKind, PipelineStage
from .stages import (
    MemoryMatch, MemoryRetrieval, RetrievalDecision, RetrievalPolicy, RetrievalSource,
    plan_memory_pin, structural_compatibility, snippet,
)
from ..content import ContentDocument, TranslationUnit
from .context.execution import ContextExecution, plan_with_context
from .context.model import ContextError
from ..jobs import JobState, TranslationJob
from .resume_reuse import REUSE_DIMENSIONS, ArtifactIntegrity, ResumeStage, StageEvidence, select_resume_stage
from ..jobs import (
    CancellationAcknowledgement, JobCorrelation, JobEvent, JobProgress)
from ..jobs import CancellationSource, JobEventSink
from ..provenance import (
    UNKNOWN, ActorKind, ConfigurationRef, ContextProvenance, ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef,
)
from ..provenance import ProvenanceRepository
from ..providers import ProviderStatus, TranslationProviderRequest, UnitReference
from ..providers import TranslationProvider, execute
from ..knowledge.memory import TranslationMemoryMatch
from .pipeline import (
    GlossaryOutcome, LinguisticOutcome, LinguisticSource, LinguisticStages, ProtectedContentError,
)
from .plan import ResolvedTranslationPlan
from ..content import TAG_TOKEN_PATTERN, TextParts, protected_content_is_intact
from ..kernel import LanguagePair, JobValidationError, validate_job_identity, JobFailure, JobFailureFamily, JobWarning

# Version 2 records the producing dimensions, so a continuation can prove or name what changed.
RAW_RESULTS_VERSION = 2
_RAW_RESULTS_VERSIONS = frozenset({1, RAW_RESULTS_VERSION})
# How far back a continuation looks for raw results. A chain this long is already unusual.
MAX_REUSABLE_JOBS = 32
_MAX_ANCESTOR_ARTIFACTS = 1024
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
    APPROVED_MEMORY = "approved_memory"
    PROVIDER = "provider"
    CACHE_ONLY_MISSING = "cache_only_missing"
    DRY_RUN = "dry_run"
    ARTIFACT_REPLAY = "artifact_replay"


_FROM_LINGUISTIC = {
    LinguisticSource.BLANK: UnitSource.BLANK,
    LinguisticSource.EXACT_CACHE: UnitSource.EXACT_CACHE,
    LinguisticSource.APPLIED_MEMORY: UnitSource.APPLIED_MEMORY,
    LinguisticSource.APPROVED_MEMORY: UnitSource.APPROVED_MEMORY,
    LinguisticSource.PROVIDER: UnitSource.PROVIDER,
    LinguisticSource.CACHE_ONLY_MISSING: UnitSource.CACHE_ONLY_MISSING,
    LinguisticSource.DRY_RUN: UnitSource.DRY_RUN,
}
_PROVENANCE_OPERATIONS = {
    UnitSource.EXACT_CACHE: ("reuse_exact_cache", SourceKind.CACHE, ActorKind.SYSTEM),
    UnitSource.APPROVED_MEMORY: ("reuse_approved_memory", SourceKind.TRANSLATION_MEMORY, ActorKind.SYSTEM),
    UnitSource.APPLIED_MEMORY: ("reuse_legacy_cache", SourceKind.CACHE, ActorKind.SYSTEM),
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


def _decision_metadata(decision: RetrievalDecision, source: UnitSource) -> dict:
    key = decision.cache_identity
    scope = decision.scope
    return {
        "source": decision.source.value, "auto_applied": decision.auto_applied,
        "reason": decision.reason, "score": decision.score,
        "entry_id": decision.entry_id, "revision_id": decision.revision_id,
        "provenance_id": decision.provenance_id, "revision_number": decision.revision_number,
        "cache_identity": None if key is None else {
            "scope_kind": key.scope.scope.kind.value, "scope_id": key.scope.scope.scope_id,
            "value_kind": key.value_kind.value, "semantic_fingerprint": key.semantic_fingerprint,
            "source_content_digest": key.source_content_digest,
        },
        "lifecycle": decision.lifecycle.value, "reviewed": decision.reviewed, "dataset_eligible": False,
        "value_stage": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "pre_glossary",
        "output_kind": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "glossary_derived",
        "output_stage": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "terminology_applied",
        "approval_inherited_after_transformation": False,
        "scope": (None if scope is None else [scope.kind.value, scope.owner_id] if isinstance(scope, MemoryScope)
                  else [scope.scope.kind.value, scope.scope.scope_id]),
    }


def _canonical_value(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _canonical_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _dimension_digest(value: object) -> str:
    data = json.dumps(_canonical_value(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("ascii")).hexdigest()


def reuse_dimensions(document: ContentDocument, plan: ResolvedTranslationPlan) -> dict[str, str]:
    """One opaque digest per ``REUSE_DIMENSIONS`` name, from what produced raw text for this document."""
    producer = plan.producer
    raw_stages = [[item.stage_id, item.version] for item in plan.stages if item.stage_id != "post_processing"]
    values = {
        "input": [document.revision_id, document.source_fingerprint, document.source_locale],
        "configuration": [plan.policy_ref, plan.configuration_revision, plan.glossary_ref if plan.glossary_native else None,
                          plan.context_ref, plan.context_strategy, plan.context_revision, plan.prompt_strategy,
                          plan.prompt_revision, plan.terminology_revision, plan.generation_parameters],
        "provider": [producer.provider, producer.model, producer.adapter],
        # Only the languages: each hop's producer and capabilities are already their own dimensions.
        "route": [plan.route.source, plan.route.intermediate, plan.route.target,
                  None if plan.route_hops is None else [[hop.source_locale, hop.target_locale] for hop in plan.route_hops]],
        "capability": [plan.capabilities, plan.chunk_limit],
        "format": [document.adapter, plan.format_constraints],
        "locator": [document.project_id, document.document_id, document.logical_key,
                    [unit.unit_id for unit in document.units]],
        "pipeline": [producer.pipeline, plan.pipeline_revision, plan.segmentation_revision, plan.chunking_revision,
                     plan.protection_revision, plan.normalization_revision, plan.schema_version],
        "stage": raw_stages,
    }
    assert tuple(values) == REUSE_DIMENSIONS
    return {name: _dimension_digest(value) for name, value in values.items()}


def _raw_payload(document: ContentDocument, plan: ResolvedTranslationPlan,
                 raw: list[tuple[str, UnitSource, str | None]], results=()) -> bytes:
    decisions = {result.unit_id: result.retrieval_decision for result in results}
    def entry(unit_id, source, text):
        value = {"raw": text, "source": source.value, "unit_id": unit_id}
        decision = decisions.get(unit_id)
        if decision is not None:
            value["decision"] = _decision_metadata(decision, source)
        return value
    return json.dumps({
        "dimensions": reuse_dimensions(document, plan),
        "document_id": document.document_id,
        "raw_fingerprint": plan.raw_fingerprint,
        "revision_id": document.revision_id,
        "schema_version": RAW_RESULTS_VERSION,
        "units": [entry(unit_id, source, text) for unit_id, source, text in raw],
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _json_object(payload: bytes | None) -> dict | None:
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    return data if type(data) is dict else None


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
                 memory: MemoryPort | None = None, approved_memory: MemoryRetrieval | None = None,
                 retrieval_policy: RetrievalPolicy = RetrievalPolicy(), clock: Callable[[], datetime],
                 translation_context: ContextExecution | None = None,
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
        self._approved_memory = approved_memory
        self._retrieval_policy = approved_memory.policy if approved_memory is not None else retrieval_policy
        self._clock = clock
        self._new_id = new_id
        self._translation_context = translation_context
        self._ancestors: dict[str, dict[str, Artifact]] = {}

    def __repr__(self) -> str:
        return "DocumentTranslator()"

    def translate(self, document: ContentDocument, context: ExecutionContext, *, dry_run: bool = False,
                  cache_only: CacheOnlyRequest | None = None, replay_raw_results: bool = True) -> DocumentTranslationResult:
        if cache_only is not None and type(cache_only) is not CacheOnlyRequest:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "cache_only_mode_required"))
        self._check_identity(document, context)
        if self._translation_context is not None:
            try:
                self._translation_context.validate(self._clock())
                # Todas as unidades, inclusive as de um cache pin, precisam do
                # binding exato antes da primeira leitura de qualquer porta.
                for unit in document.units:
                    self._translation_context.for_unit(document, unit, self._clock(), self._plan)
            except ContextError as error:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, error.code)) from None
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
        # Retrieval eligibility/lifecycle must be checked again on every run.
        replayed = self._replay(run) if (replay_raw_results and self._approved_memory is None
                                       and not self._retrieval_policy.legacy_cache_enabled
                                       and self._translation_context is None) else None
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
        reusable = context.reusable_jobs
        if (type(reusable) is not tuple or len(reusable) > MAX_REUSABLE_JOBS or len(set(reusable)) != len(reusable)
                or job.job_id in reusable):
            raise refuse("invalid_reusable_jobs")
        for job_id in reusable:
            try:
                validate_job_identity(job_id)
            except JobValidationError:
                raise refuse("invalid_reusable_jobs") from None
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
        if self._approved_memory is not None or self._retrieval_policy.legacy_cache_enabled or self._translation_context is not None:
            # Never replay a final TM value as pre-glossary raw text, or bypass review withdrawal.
            identity = json.dumps([identity, "retrieval_revalidated", execution_nonce], separators=(",", ":"))
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
            return self._replay_from_chain(run)
        artifact = found.artifact
        try:
            with self._artifacts.open_reusable(artifact.artifact_id, request) as stream:
                payload = stream.read()
        except (ArtifactIntegrityError, ArtifactPathError):
            self._artifacts.retire_if_corrupt(artifact.artifact_id)
            return self._replay_from_chain(run)
        raw = self._decode_raw(payload, run.document, run.cache_scope)
        results = []
        for index, unit in enumerate(run.document.units):
            run.checkpoint("pipeline_before_unit")
            results.append(run.apply_raw(unit, raw[index]))
            run.progress(index + 1)
        self._record_replay(run, artifact)
        return run.result(tuple(results), artifact.artifact_id)

    def _chain_artifacts(self, project_id: str, job_id: str) -> dict[str, Artifact]:
        """The newest ready raw-results record of each document in one earlier job. Read only, once per job."""
        known = self._ancestors.get(job_id)
        if known is not None:
            return known
        found: dict[str, Artifact] = {}
        try:
            artifacts = self._artifacts.list_ready(job_id, limit=_MAX_ANCESTOR_ARTIFACTS)
        except ArtifactError:
            artifacts = ()
        for artifact in artifacts:
            request = artifact.request
            if ((request.project_id, request.job_id, request.role, request.stage, request.media_type)
                    != (project_id, job_id, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, RAW_RESULTS_MEDIA_TYPE)):
                continue
            payload = self._read_chain_payload(artifact)
            data = _json_object(payload)
            if data is not None and type(data.get("document_id")) is str:
                found[data["document_id"]] = artifact  # Oldest first, so a redone document keeps its newest record.
        self._ancestors[job_id] = found
        return found

    def _read_chain_payload(self, artifact: Artifact) -> bytes | None:
        """Verified bytes of an earlier job's record, or None. That job is ended and its records are never changed."""
        try:
            with self._artifacts.open_reusable(artifact.artifact_id, artifact.request) as stream:
                return stream.read()
        except (ArtifactIntegrityError, ArtifactPathError, ArtifactError, OSError):
            return None

    def _replay_from_chain(self, run: _DocumentRun) -> DocumentTranslationResult | None:
        """Reuse an earlier job's raw results only when ``select_resume_stage`` proves every dimension equal."""
        reusable = run.context.reusable_jobs
        if not reusable:
            return None
        document = run.document
        for job_id in reusable:
            artifact = self._chain_artifacts(document.project_id, job_id).get(document.document_id)
            if artifact is None:
                continue  # This job never finished this document; an earlier one may have.
            payload = self._read_chain_payload(artifact)
            data = _json_object(payload)
            schema_valid = (data is not None and type(data.get("schema_version")) is int
                            and data["schema_version"] in _RAW_RESULTS_VERSIONS and data.get("document_id") == document.document_id)
            recorded = data.get("dimensions") if schema_valid else None
            produced = (tuple((name, recorded.get(name)) for name in REUSE_DIMENSIONS)
                        if type(recorded) is dict and all(type(value) is str for value in recorded.values()) else ())
            evidence = (
                StageEvidence(ResumeStage.TRANSLATE,
                              ArtifactIntegrity.CORRUPT if payload is None else ArtifactIntegrity.VERIFIED,
                              owned=artifact.request.job_id == job_id and artifact.request.project_id == document.project_id,
                              schema_valid=schema_valid, expected=tuple(reuse_dimensions(document, self._plan).items()),
                              produced=produced),
                # The output is always built again from the replayed raw results.
                StageEvidence(ResumeStage.RECONSTRUCT, ArtifactIntegrity.MISSING),
            )
            selection = select_resume_stage(evidence)
            if selection.resume_from is not ResumeStage.RECONSTRUCT:
                run.emit(JobWarning(f"raw_results_{selection.reason.value}"))
                for name in selection.changed:
                    run.emit(JobWarning(f"raw_results_changed_{name}"))
                return None
            try:
                raw = self._decode_raw(payload, document, run.cache_scope)
            except TranslationExecutionError:
                # Every recorded dimension matched, so something unrecorded differs. Prove nothing and redo.
                run.emit(JobWarning("raw_results_provenance_unproven"))
                return None
            results = []
            for index, unit in enumerate(document.units):
                run.checkpoint("pipeline_before_unit")
                results.append(run.apply_raw(unit, raw[index]))
                run.progress(index + 1)
            self._record_replay(run, artifact)
            return run.result(tuple(results), artifact.artifact_id)
        return None

    def _decode_raw(self, payload: bytes, document: ContentDocument, scope: AuthorizedCacheScope):
        invalid = TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "invalid_raw_results"))
        try:
            data = json.loads(payload)
            keys = {"document_id", "raw_fingerprint", "revision_id", "schema_version", "units"}
            if (type(data) is not dict or data.get("schema_version") not in _RAW_RESULTS_VERSIONS
                    or set(data) != (keys if data["schema_version"] == 1 else keys | {"dimensions"})
                    or type(data["schema_version"]) is not int or data["document_id"] != document.document_id
                    or data["revision_id"] != document.revision_id or data["raw_fingerprint"] != self._plan.raw_fingerprint
                    or type(data["units"]) is not list or len(data["units"]) != len(document.units)):
                raise invalid
            decoded = []
            for unit, entry in zip(document.units, data["units"]):
                if (type(entry) is not dict or set(entry) not in ({"raw", "source", "unit_id"}, {"raw", "source", "unit_id", "decision"})
                        or entry["unit_id"] != unit.unit_id):
                    raise invalid
                source, raw = UnitSource(entry["source"]), entry["raw"]
                template, _ = _template(unit)
                core = TextParts.from_text(template).core
                if source is UnitSource.BLANK:
                    if raw is not None or core:
                        raise invalid
                elif (source not in (UnitSource.EXACT_CACHE, UnitSource.PROVIDER) or not isinstance(raw, str)
                        or not protected_content_is_intact(core, raw)):
                    raise invalid
                decision = None
                if "decision" in entry:
                    if source not in (UnitSource.EXACT_CACHE, UnitSource.PROVIDER):
                        raise invalid
                    cached = source is UnitSource.EXACT_CACHE
                    fingerprint = self._plan.semantic_fingerprint
                    if cached and fingerprint is None:
                        raise invalid
                    decision = RetrievalDecision(RetrievalSource.EXACT_CACHE if cached else RetrievalSource.PROVIDER,
                        True, "eligible_exact_cache" if cached else "no_eligible_reuse", scope=scope if cached else None,
                        cache_identity=exact_entry_key(scope, TRANSLATION_STAGE_AGGREGATE, fingerprint, unit.source_text)
                                       if cached else None)
                    if entry["decision"] != _decision_metadata(decision, source):
                        raise invalid
                decoded.append((source, raw, decision))
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
        artifact = run.publish(raw, tuple(results))
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
        for unit_id, context in run.contexts.items():
            self._provenance.append(ProvenanceRequest(
                document.project_id, job.job_id, 'build_translation_context', ActorKind.SYSTEM, SourceKind.PIPELINE,
                self._plan.producer, subject=SubjectRef('translation_unit', 1, unit_id),
                artifacts=(artifact.artifact_id,), context_metadata=ContextProvenance(context.policy.value,
                    tuple((e.kind.value, e.revision) for e in context.selected),
                    tuple((o.source.kind.value, o.reason.value) for o in context.omitted), context.disclosure.utf8_bytes),
                idempotency_key=self._idempotency_key(run, 'build_translation_context', artifact.artifact_id + unit_id)))
        subject = SubjectRef("content_revision", 1, document.revision_id)
        parents = []
        for source, (operation, source_kind, actor) in _PROVENANCE_OPERATIONS.items():
            if not any(item[1] is source for item in raw):
                continue
            producer = _UNKNOWN_PRODUCER if source in (UnitSource.APPLIED_MEMORY, UnitSource.APPROVED_MEMORY) else self._plan.producer
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

    def __init__(self, translator: DocumentTranslator, run: _DocumentRun, unit: TranslationUnit, plan=None, context=None) -> None:
        self._translator = translator
        self._run = run
        self._unit = unit
        self._plan = plan or translator._plan
        self._context = context

    def translate(self, text: str, source: str, target: str) -> str:
        self._run.checkpoint("pipeline_between_chunks")
        plan = self._plan
        document = self._run.document
        if self._context is not None:
            try:
                execution = self._translator._translation_context
                execution.for_unit(document, self._unit, self._translator._clock(), self._translator._plan)
                execution.authorize(self._context, self._translator._clock())
            except ContextError as error:
                raise _ProviderStopped(JobFailure(JobFailureFamily.INPUT, error.code)) from None
        request = TranslationProviderRequest(
            self._translator._new_id(), text, source, target, route=plan.route,
            unit=UnitReference(document.document_id, document.revision_id, self._unit.unit_id),
            policy_ref=plan.policy_ref,
            features=() if self._context is None or self._context.feature is None else (self._context.feature,))
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
        from ..knowledge.cache import CACHE_ENTRY_V2_LIMITS
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
        from ..knowledge.cache import CACHE_ENTRY_V2_LIMITS
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
    def __init__(self, memory: MemoryPort, plan: ResolvedTranslationPlan, scope: AuthorizedCacheScope, limit=20) -> None:
        self._memory = memory
        self._limit = limit
        self.last = None
        self._plan = plan
        self._scope = scope

    def lookup(self, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None:
        fingerprint = self._plan.semantic_fingerprint
        if fingerprint is None:
            return None
        query = CompatibleMemoryQuery(
            self._scope, TRANSLATION_STAGE_AGGREGATE, fingerprint, text, self._limit,
        )
        try:
            candidate = self._memory.lookup(query)
        except CacheIdentityError:
            raise
        except Exception:
            raise CacheIdentityError("memory_repository_failed") from None
        if candidate is None or not candidate_matches_query(candidate, query):
            return None
        self.last = candidate
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
        self.contexts = {}

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
        context = None
        if translator._translation_context is not None:
            try:
                context = translator._translation_context.for_unit(self.document, unit, translator._clock(), plan)
                plan = plan_with_context(plan, context)
                self.contexts[unit.unit_id] = context
            except ContextError as error:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, error.code)) from None
            for warning in context.warnings:
                self.emit(warning)
        template, originals = _template(unit)
        legacy_memory = (None if (translator._memory is None or not translator._retrieval_policy.legacy_cache_enabled)
            else _PlanBoundMemory(translator._memory, plan, self.cache_scope, translator._retrieval_policy.limits.candidates))
        stages = LinguisticStages(
            _ContractProvider(translator, self, unit, plan, context),
            _PlanBoundCache(translator._exact_cache, plan, self.cache_scope, self.cache_only, self.resolved_pin,
                            unit.source_text),
            translator._apply_glossary,
            memory=legacy_memory,
            approved=None if translator._approved_memory is None else lambda text, pair: self._approved(unit, pair),
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
        except MemoryError:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "memory_retrieval_failed")) from None
        source = _FROM_LINGUISTIC[outcome.source]
        decision = outcome.retrieval_decision
        if source in (UnitSource.EXACT_CACHE, UnitSource.APPLIED_MEMORY, UnitSource.PROVIDER):
            origin = {UnitSource.EXACT_CACHE: RetrievalSource.EXACT_CACHE,
                      UnitSource.APPLIED_MEMORY: RetrievalSource.LEGACY_CACHE,
                      UnitSource.PROVIDER: RetrievalSource.PROVIDER}[source]
            reason = ("eligible_exact_cache" if source is UnitSource.EXACT_CACHE else
                      "explicit_legacy_compatibility" if source is UnitSource.APPLIED_MEMORY else
                      "no_eligible_reuse")
            decision = RetrievalDecision(origin, True, reason,
                score=None if legacy_memory is None or legacy_memory.last is None or source is not UnitSource.APPLIED_MEMORY else legacy_memory.last.score,
                scope=self.cache_scope if source is not UnitSource.PROVIDER else None,
                cache_identity=None if source is UnitSource.PROVIDER or plan.semantic_fingerprint is None else
                    exact_entry_key(self.cache_scope, TRANSLATION_STAGE_AGGREGATE, plan.semantic_fingerprint,
                        unit.source_text if source is UnitSource.EXACT_CACHE else legacy_memory.last.original))
        suggestions = outcome.retrieval_suggestions
        if outcome.memory_suggestion is not None:
            match = outcome.memory_suggestion
            legacy_decision = RetrievalDecision(RetrievalSource.LEGACY_CACHE, False, "legacy_review_required", match.score,
                self.cache_scope, cache_identity=None if plan.semantic_fingerprint is None else
                    exact_entry_key(self.cache_scope, TRANSLATION_STAGE_AGGREGATE, plan.semantic_fingerprint, match.original))
            suggestions += (MemoryMatch(match.original, match.translated, snippet(match.original),
                                        snippet(match.translated), legacy_decision),)
        if suggestions:
            self.emit(JobWarning("memory_review_suggestions"))
        # An untranslated unit expands back to its exact source text.
        return UnitTranslationResult(unit.unit_id, source, _expand(outcome.text, originals), bool(suggestions),
                                     outcome.glossary_usage, outcome.text, outcome.cache_lookup_state,
                                     outcome.cache_miss_reason, decision, suggestions,
                                     ValueKind.LINGUISTIC_FINAL if source is UnitSource.APPROVED_MEMORY else
                                         ValueKind.GLOSSARY_DERIVED if decision is not None else None,
                                     PipelineStage.LINGUISTIC_FINAL if source is UnitSource.APPROVED_MEMORY else
                                         PipelineStage.TERMINOLOGY_APPLIED if decision is not None else None), outcome

    def _approved(self, unit: TranslationUnit, pair: LanguagePair) -> tuple[MemoryMatch, ...]:
        memory = self.translator._approved_memory
        if unit.content.protected_parts:
            return ()
        policy = self.translator._retrieval_policy
        if max(len(unit.source_text.encode('utf-8')),
               len(unicodedata.normalize('NFC', unit.source_text).encode('utf-8'))) > policy.limits.source_bytes:
            return ()
        options = dict(text=unit.source_text, languages=pair,
            scopes=(MemoryScope(ScopeKind.PROJECT, self.document.project_id),),
            pin=plan_memory_pin(self.translator._plan), limits=policy.limits)
        def compatible(revision):
            source = revision.value.source
            if (self.translator._plan.semantic_fingerprint is None
                    or (source.project_id, source.document_id, source.document_revision) !=
                       (self.document.project_id, self.document.document_id, self.document.revision_id)):
                return None  # Missing structural evidence: an unapproved review suggestion only.
            return structural_compatibility(self.document, unit, revision)
        exact = memory.query(MemoryQuery(**options), compatible=compatible)
        if any(match.decision.auto_applied for match in exact):
            return exact
        fuzzy = memory.query(MemoryQuery(**options, mode=RetrievalMode.FUZZY), compatible=compatible)
        # Exact suggestions remain visible; they never shadow a compatible fuzzy hit.
        ids = {match.decision.revision_id for match in exact}
        merged = list(exact) + [match for match in fuzzy if match.decision.revision_id not in ids]
        merged.sort(key=lambda match: (not match.decision.auto_applied, -match.decision.score,
                                       match.decision.entry_id, match.decision.revision_id))
        bounded, used = [], 0
        for match in merged:
            size = len(match.source.encode('utf-8')) + len(match.target.encode('utf-8'))
            if len(bounded) < policy.limits.candidates and used + size <= policy.limits.aggregate_bytes:
                bounded.append(match)
                used += size
        return tuple(bounded)

    def apply_raw(self, unit: TranslationUnit, entry) -> UnitTranslationResult:
        """Only post-processing runs again: no cache lookup and no provider."""
        source, raw, decision = entry
        if source is UnitSource.BLANK:
            return UnitTranslationResult(unit.unit_id, UnitSource.BLANK, unit.source_text, tokenized_text=_template(unit)[0])
        template, originals = _template(unit)
        parts = TextParts.from_text(template)
        application = self.translator._apply_glossary(raw)
        tokenized = parts.restore(application.text)
        return UnitTranslationResult(unit.unit_id, UnitSource.ARTIFACT_REPLAY, _expand(tokenized, originals),
                                     False, application.usage, tokenized, retrieval_decision=decision,
                                     value_kind=ValueKind.GLOSSARY_DERIVED, value_stage=PipelineStage.TERMINOLOGY_APPLIED)

    def publish(self, raw: list[tuple[str, UnitSource, str | None]], results=()) -> Artifact:
        translator = self.translator
        source = self.context.cancellation
        try:
            return translator._artifacts.publish(
                translator._artifact_request(self.document, self.context.job, self.execution_nonce),
                (_raw_payload(self.document, translator._plan, raw, results),),
                owner_token=self.context.owner_token, cancellation=None if source is None else source.token)
        except ArtifactCancelledError:
            self._stop(source.acknowledgement)
        except ArtifactError as error:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTERNAL, str(error))) from None

    def result(self, units: tuple[UnitTranslationResult, ...], artifact_id: str | None) -> DocumentTranslationResult:
        return DocumentTranslationResult(self.document.document_id, self.document.revision_id, units, artifact_id, self.sequence,
                                         self.translator._plan.cache_eligibility)
