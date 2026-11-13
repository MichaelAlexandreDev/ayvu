"""Durable exact-cache reconstruction, with no production or replay ports.

Each unit resolves its own canonical plan. The transient unit kernel owns lookup,
protection and glossary; this executor adds durable authority and reuse lineage.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from ..jobs import JobState, JobEvent, JobCorrelation
from ..kernel import JobFailure, JobFailureFamily, LanguagePair, JobWarning
from ..knowledge.cache import (
    CacheAuthorizationPolicy, CacheIdentityError, CacheOnlyMode, CacheOnlyRequest,
    ResolvedCacheExecutionIdentity, TRANSLATION_STAGE_AGGREGATE,
)
from ..provenance import ActorKind, ConfigurationRef, ProvenanceRequest, SourceKind, SubjectRef
from .document_contracts import TranslationCancelled, TranslationExecutionError, _expand, _template
from .engine import STATUS_MISSING
from .local_cache_plan import LocalCachePlanResolver, LocalCachePlanState
from .transient_units import TransientUnits


class _PinnedResolver:
    """A pin narrows canonical evidence; it never falls back to compatible lookup."""

    def __init__(self, resolver, pin):
        self.resolver, self.pin = resolver, pin

    def resolve(self, source):
        return self.resolver.resolve(source, pinned=self.pin)


class CacheOnlyDocumentExecutor:
    """Prepare whole documents so fatal errors escape the engine's v1 unit tolerance.

    ``resolve_pin`` is an optional durable job/provenance authority, never a label
    or a cache repository. Without that authority a pinned request stays missing.
    """

    def __init__(self, reader, intent, context, provenance, readable_original, apply_glossary, *,
                 clock, checkpoint=lambda: None, begin_unit=lambda index: None, request=CacheOnlyRequest.resolve_compatible(), resolve_pin=None, map_execution=lambda unit, result: result):
        if type(request) is not CacheOnlyRequest:
            raise CacheIdentityError("cache_only_mode_required")
        self.context, self.provenance, self.clock = context, provenance, clock
        self.map_execution = map_execution
        self.checkpoint, self.begin_unit = checkpoint, begin_unit
        self._sequence = 0
        self.source_locale = intent.source_locale
        self.scope = CacheAuthorizationPolicy(context.job.request.project_id).resolve()
        resolver = LocalCachePlanResolver(reader, self.scope, intent)
        if request.mode is CacheOnlyMode.PINNED:
            pin = None if resolve_pin is None else resolve_pin(request.pin, self.scope)
            if pin is not None:
                if type(pin) is not ResolvedCacheExecutionIdentity:
                    raise CacheIdentityError("invalid_resolved_identity")
                if pin.scope != self.scope:
                    raise CacheIdentityError("cache_scope_denied")
            resolver = None if pin is None or pin.value_kind is not TRANSLATION_STAGE_AGGREGATE else _PinnedResolver(resolver, pin)
        self.units = TransientUnits(LanguagePair(source=intent.source_locale, target=intent.target_locale), apply_glossary, readable_original,
            reader=reader, resolver=resolver, cache_only=True, chunk_limit=intent.chunk_limit,
            checkpoint=self._checkpoint, on_hit=self._record_hit, on_resolution=self._notice)
        self._executions = {}

    def __repr__(self):
        return "CacheOnlyDocumentExecutor()"

    def _checkpoint(self, stage):
        self.checkpoint()
        job = self.context.current_job() if self.context.current_job is not None else self.context.job
        if (job.state is not JobState.RUNNING or job.lease is None
                or self.context.job.lease is None or job.job_id != self.context.job.job_id
                or job.request.project_id != self.scope.project_id
                or job.lease.owner_token != self.context.job.lease.owner_token or job.lease.expired(self.clock())):
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "lease_not_held"))
        source = self.context.cancellation
        point = "pipeline_before_unit" if stage == "unit_whitespace" else "pipeline_between_stages"
        if source is not None and source.token.checkpoint(point) is not None:
            raise TranslationCancelled()

    def execute_document(self, document, index=0):
        self._executions = {}
        self._checkpoint("unit_whitespace")
        if document.project_id != self.scope.project_id:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "cache_scope_denied"))
        if any(locale.value is not None and locale.value != self.source_locale
               for locale in (document.source_locale, *(unit.content.source_locale for unit in document.units))):
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "locale_conflicts_with_plan"))
        executions = {}
        try:
            for unit_index, unit in enumerate(document.units):
                self.begin_unit(unit_index)
                self._checkpoint("unit_whitespace")
                readable = _expand if unit.content.kind.name in {"title", "alt_text"} else None
                result = self.units.execute(unit, readable_original=readable)
                if result.status == STATUS_MISSING and not result.missing_text.strip():
                    raise CacheIdentityError("empty_missing_original")
                executions[unit.unit_id] = self.map_execution(unit, result)
        except CacheIdentityError as error:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, error.code)) from None
        self._executions = executions
        return tuple(executions[unit.unit_id] for unit in document.units)

    def _record_hit(self, unit, resolution):
        self._checkpoint("reuse_provenance")
        key = resolution.key
        # The complete exact key is the stable entry address, including its scope.
        address = json.dumps([key.scope.project_id, key.scope.scope.kind.value, key.scope.scope.scope_id, key.value_kind.value,
                              key.semantic_fingerprint, key.source_content_digest], separators=(",", ":"))
        reference = "cache_entry:" + hashlib.sha256(address.encode("utf-8")).hexdigest()
        job = self.context.job
        self.provenance.append(ProvenanceRequest(
            key.scope.project_id, job.job_id, "reuse_exact_cache", ActorKind.SYSTEM, SourceKind.CACHE,
            resolution.plan.producer, subject=SubjectRef("translation_unit", 1, unit.unit_id),
            outcome_code="exact_cache_validated", configuration_refs=(ConfigurationRef("exact_cache", reference),),
            idempotency_key=f"cache:{job.job_id}:{unit.unit_id}"))

    def _notice(self, resolution):
        if resolution is None or resolution.state not in {LocalCachePlanState.AMBIGUOUS, LocalCachePlanState.UNSUPPORTED}:
            return
        code = "cache_identity_ambiguous" if resolution.state is LocalCachePlanState.AMBIGUOUS else resolution.reason
        self._sequence += 1
        self.context.events.emit(JobEvent(str(uuid4()), self.context.job.job_id, self._sequence, self.clock(),
            JobCorrelation(self.scope.project_id), JobWarning(code)))

    def execute(self, unit):
        return self._executions[unit.unit_id]

    def untranslated(self, unit):
        return _template(unit)[0]
