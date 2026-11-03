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
    """Keys on the plan, so entries cannot cross provider or pipeline identity.

    The legacy v1 cache has no such key and does not satisfy this port.
    """

    def get(self, plan_fingerprint: str, text: str, language_pair: LanguagePair) -> str | None: ...
    def set(self, plan_fingerprint: str, text: str, language_pair: LanguagePair, translated: str) -> None: ...


class MemoryPort(Protocol):
    """Keys on the plan as well. The cache-backed legacy memory does not satisfy this port."""

    def lookup(self, plan_fingerprint: str, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None: ...


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

    def __repr__(self) -> str:
        return f"UnitTranslationResult(unit_id={self.unit_id!r}, source={self.source.value!r})"


@dataclass(frozen=True, repr=False)
class DocumentTranslationResult:
    document_id: str
    revision_id: str
    units: tuple[UnitTranslationResult, ...]
    artifact_id: str | None
    next_sequence: int

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
    if TAG_TOKEN_PATTERN.search(text):
        raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "reserved_placeholder_in_source"))
    pieces: list[str] = []
    originals: list[str] = []
    cursor = 0
    for part in unit.content.protected_parts:
        pieces.append(text[cursor:part.start])
        pieces.append(f"__AYVU_TAG_{len(originals)}__")
        originals.append(text[part.start:part.end])
        cursor = part.end
    pieces.append(text[cursor:])
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
                  cache_only: bool = False) -> DocumentTranslationResult:
        self._check_identity(document, context)
        run = _DocumentRun(self, document, context)
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

    def _artifact_request(self, document: ContentDocument, job: TranslationJob) -> ArtifactRequest:
        version = next(item.version for item in self._plan.stages if item.stage_id == RAW_RESULTS_STAGE)
        identity = json.dumps([document.document_id, document.revision_id, self._plan.raw_fingerprint], separators=(",", ":"))
        return ArtifactRequest(document.project_id, job.job_id, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, version,
                               f"sha256:{hashlib.sha256(identity.encode('ascii')).hexdigest()}", RAW_RESULTS_MEDIA_TYPE)

    def _replay(self, run: _DocumentRun) -> DocumentTranslationResult | None:
        """Reuse raw results only when every identity and integrity check passes."""
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

    def _execute(self, run: _DocumentRun, *, dry_run: bool, cache_only: bool) -> DocumentTranslationResult:
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
        identity = json.dumps([run.context.job.job_id, run.document.revision_id, self._plan.raw_fingerprint, operation, extra],
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
    def __init__(self, cache: ExactCachePort | None, fingerprint: str) -> None:
        self._cache = cache
        self._fingerprint = fingerprint

    def get(self, text: str, language_pair: LanguagePair) -> str | None:
        return None if self._cache is None else self._cache.get(self._fingerprint, text, language_pair)

    def set(self, text: str, language_pair: LanguagePair, translated: str) -> None:
        if self._cache is not None:
            self._cache.set(self._fingerprint, text, language_pair, translated)


class _PlanBoundMemory:
    def __init__(self, memory: MemoryPort, fingerprint: str) -> None:
        self._memory = memory
        self._fingerprint = fingerprint

    def lookup(self, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None:
        return self._memory.lookup(self._fingerprint, text, language_pair)


class _DocumentRun:
    def __init__(self, translator: DocumentTranslator, document: ContentDocument, context: ExecutionContext) -> None:
        self.translator = translator
        self.document = document
        self.context = context
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

    def translate_unit(self, unit: TranslationUnit, *, dry_run: bool, cache_only: bool) -> tuple[UnitTranslationResult, LinguisticOutcome]:
        translator = self.translator
        plan = translator._plan
        template, originals = _template(unit)
        stages = LinguisticStages(
            _ContractProvider(translator, self, unit), _PlanBoundCache(translator._exact_cache, plan.raw_fingerprint),
            translator._apply_glossary,
            memory=None if translator._memory is None else _PlanBoundMemory(translator._memory, plan.raw_fingerprint),
            on_stage=self._stage_entered)
        try:
            outcome = stages.run(template, LanguagePair(source=plan.route.source, target=plan.route.target),
                                 dry_run=dry_run, cache_only=cache_only, chunk_limit=plan.chunk_limit)
        except _ProviderStopped as stopped:
            raise TranslationExecutionError(stopped.failure) from None
        except ProtectedContentError:
            # Stage messages are for the current CLI; this path reports a code and no text.
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "protected_content_not_preserved")) from None
        source = _FROM_LINGUISTIC[outcome.source]
        # An untranslated unit expands back to its exact source text.
        return UnitTranslationResult(unit.unit_id, source, _expand(outcome.text, originals), outcome.memory_suggestion is not None,
                                     outcome.glossary_usage), outcome

    def apply_raw(self, unit: TranslationUnit, entry: tuple[UnitSource, str | None]) -> UnitTranslationResult:
        """Only post-processing runs again: no cache lookup and no provider."""
        source, raw = entry
        if source is UnitSource.BLANK:
            return UnitTranslationResult(unit.unit_id, UnitSource.BLANK, unit.source_text)
        template, originals = _template(unit)
        parts = TextParts.from_text(template)
        application = self.translator._apply_glossary(raw)
        return UnitTranslationResult(unit.unit_id, UnitSource.ARTIFACT_REPLAY, _expand(parts.restore(application.text), originals),
                                     False, application.usage)

    def publish(self, raw: list[tuple[str, UnitSource, str | None]]) -> Artifact:
        translator = self.translator
        source = self.context.cancellation
        try:
            return translator._artifacts.publish(
                translator._artifact_request(self.document, self.context.job), (_raw_payload(self.document, translator._plan, raw),),
                owner_token=self.context.owner_token, cancellation=None if source is None else source.token)
        except ArtifactCancelledError:
            self._stop(source.acknowledgement)
        except ArtifactError as error:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTERNAL, str(error))) from None

    def result(self, units: tuple[UnitTranslationResult, ...], artifact_id: str | None) -> DocumentTranslationResult:
        return DocumentTranslationResult(self.document.document_id, self.document.revision_id, units, artifact_id, self.sequence)
