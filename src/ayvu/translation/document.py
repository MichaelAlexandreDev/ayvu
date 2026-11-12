"""Translates one document with real project, job and unit identity.

Results are per unit, but persistence is per document. A single edit gives a document new
unit IDs anyway, so replay is document-wide in practice, and one artifact and one record per
unit would pass the project database's limit on a long book. The raw results of each unit
live inside the document's artifact.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import uuid4

from ..artifacts import Artifact, ArtifactCancelledError, ArtifactError, ArtifactRequest
from ..artifacts import ArtifactService
from ..knowledge.cache import (
    AuthorizedCacheScope, CacheAuthorizationPolicy, CacheIdentityError, CacheOnlyMode, CacheOnlyRequest,
    ResolvedCacheExecutionIdentity, TRANSLATION_STAGE_AGGREGATE, exact_entry_key,
)
from ..knowledge.memory.approved import MemoryError, ValueKind, PipelineStage
from .stages import MemoryMatch, MemoryRetrieval, RetrievalDecision, RetrievalPolicy, RetrievalSource, snippet
from ..content import ContentDocument, TranslationUnit
from .context.execution import ContextExecution, plan_with_context
from .context.model import ContextError
from ..jobs import JobState, TranslationJob
from ..jobs import CancellationAcknowledgement, JobCorrelation, JobEvent, JobProgress
from ..jobs import JobEventSink
from ..provenance import ConfigurationRef
from ..provenance import ProvenanceRepository
from ..providers import TranslationProvider
from .pipeline import (
    GlossaryOutcome, LinguisticOutcome, LinguisticSource, LinguisticStages, ProtectedContentError,
)
from .plan import ResolvedTranslationPlan
from ..content import TextParts
from ..kernel import (
    LanguagePair, JobValidationError, validate_job_identity, JobFailure, JobFailureFamily, JobWarning,
)
from .document_contracts import (
    DocumentTranslationResult, ExactCachePort, ExecutionContext, MAX_REUSABLE_JOBS, MemoryPort,
    TranslationCancelled, TranslationExecutionError, UnitSource, UnitTranslationResult, _ProviderStopped,
    _expand, _template,
)
from .raw_results import _artifact_request, _decode_raw, _raw_payload
from .document_replay import _chain_artifacts, _read_chain_payload, _replay, _replay_from_chain
from .document_provenance import _idempotency_key, _record_production, _record_replay, _references
from .document_bindings import _ContractProvider, _PlanBoundCache, _PlanBoundMemory, _approved


_FROM_LINGUISTIC = {
    LinguisticSource.BLANK: UnitSource.BLANK,
    LinguisticSource.EXACT_CACHE: UnitSource.EXACT_CACHE,
    LinguisticSource.APPLIED_MEMORY: UnitSource.APPLIED_MEMORY,
    LinguisticSource.APPROVED_MEMORY: UnitSource.APPROVED_MEMORY,
    LinguisticSource.PROVIDER: UnitSource.PROVIDER,
    LinguisticSource.CACHE_ONLY_MISSING: UnitSource.CACHE_ONLY_MISSING,
    LinguisticSource.DRY_RUN: UnitSource.DRY_RUN,
}


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
        return _artifact_request(self, document, job, execution_nonce)

    def _replay(self, run: _DocumentRun) -> DocumentTranslationResult | None:
        return _replay(self, run)

    def _chain_artifacts(self, project_id: str, job_id: str) -> dict[str, Artifact]:
        return _chain_artifacts(self, project_id, job_id)

    def _read_chain_payload(self, artifact: Artifact) -> bytes | None:
        return _read_chain_payload(self, artifact)

    def _replay_from_chain(self, run: _DocumentRun) -> DocumentTranslationResult | None:
        return _replay_from_chain(self, run)

    def _decode_raw(self, payload: bytes, document: ContentDocument, scope: AuthorizedCacheScope):
        return _decode_raw(self, payload, document, scope)

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
        return _references(self)

    def _idempotency_key(self, run: _DocumentRun, operation: str, extra: str = "") -> str:
        return _idempotency_key(self, run, operation, extra)

    def _record_production(self, run: _DocumentRun, artifact: Artifact, raw: list) -> None:
        return _record_production(self, run, artifact, raw)

    def _record_replay(self, run: _DocumentRun, artifact: Artifact) -> None:
        return _record_replay(self, run, artifact)


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
        return _approved(self, unit, pair)

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
