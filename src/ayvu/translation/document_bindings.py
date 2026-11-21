"""Bind provider and memory ports to the current unit and resolved plan."""
from __future__ import annotations

from typing import Any

import unicodedata
from ..knowledge.cache import (
    MAX_CACHE_ONLY_CANDIDATES, AuthorizedCacheScope, CacheIdentityError, CacheIdentityResolutionState,
    CacheLookupState, CacheMissReason, CacheOnlyMode, CacheOnlyRequest, CacheProvenanceMetadata,
    CacheIdentityConflict, CompatibleMemoryQuery, ExactCacheEntry, ExactCacheLookup, PutIfAbsentResult,
    PutIfAbsentState, ResolvedCacheExecutionIdentity, TRANSLATION_STAGE_AGGREGATE, candidate_matches_query,
    exact_entry_key, resolve_compatible_identities,
)
from ..knowledge.memory.approved import MemoryQuery, MemoryScope, ScopeKind, RetrievalMode
from .stages import MemoryMatch, plan_memory_pin, structural_compatibility
from ..content import TranslationUnit
from .context.model import ContextError
from ..providers import ProviderStatus, TranslationProviderRequest, UnitReference
from ..providers import execute
from ..knowledge.memory import TranslationMemoryMatch
from .plan import ResolvedTranslationPlan
from ..kernel import LanguagePair, JobFailure, JobFailureFamily
from .document_contracts import ExactCachePort, MemoryPort, _ProviderStopped


class _ContractProvider:
    """Presents the rich provider as text calls to the stages, checking each result against the plan."""

    def __init__(self, translator: Any, run: Any, unit: TranslationUnit, plan=None, context=None) -> None:
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
        self._record(request, result)
        for warning in result.warnings:
            self._run.emit(warning)
        if result.status is not ProviderStatus.SUCCEEDED:
            raise _ProviderStopped(result.failure)
        if result.producer != plan.producer or result.route != plan.route:
            # What really ran differs from what was resolved, so a new plan is needed.
            raise _ProviderStopped(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "plan_invalidated"))
        return result.text

    def _record(self, request: TranslationProviderRequest, result) -> None:
        usage = self._translator._usage
        if usage is None:
            return
        try:
            usage.record(job_id=self._run.context.job.job_id, document_id=self._run.document.document_id,
                         unit_id=self._unit.unit_id, request_id=request.request_id, text=request.text,
                         usage=result.usage, delivery=result.delivery)
        except Exception:
            # The request was made; without its record the job's usage would be wrong, so the job stops.
            raise _ProviderStopped(JobFailure(JobFailureFamily.RESOURCE, "usage_record_failed")) from None


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
