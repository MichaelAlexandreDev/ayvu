"""Resolve each unit's plan from authorized canonical evidence, without discovery.

The resolver performs no I/O itself: the caller supplies the evidence port. It is
deliberately independent of document execution and of any production composition.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from ..knowledge.cache import (
    MAX_CACHE_ONLY_CANDIDATES, TRANSLATION_STAGE_AGGREGATE, AuthorizedCacheScope,
    CacheIdentityError, CacheIdentityResolutionState, CacheValueKind, CanonicalCacheRecord,
    CanonicalValue, ExactEntryKey, OpaqueSemanticRevision, SemanticOperationV2,
    resolve_compatible_identities, semantic_operation_from_canonical, source_content_digest,
)
from ..provenance import ProducerIdentity
from ..providers import ProviderRoute, valid_reference
from .plan import (
    CHUNKING_REVISION, LINGUISTIC_STAGES, NORMALIZATION_REVISION, PIPELINE_SEMANTIC_REVISION,
    PROTECTION_REVISION, RAW_CACHE_STAGE_IDS, SEGMENTATION_REVISION,
    ResolvedTranslationPlan, TextStage, TranslationPlanError,
)


class CanonicalCacheRecordsPort(Protocol):
    """Knowledge evidence, bounded before materialization and authorized by scope."""

    def canonical_records(self, scope: AuthorizedCacheScope, value_kind: CacheValueKind,
                          source_digest: str, *, limit: int) -> tuple[CanonicalCacheRecord, ...]: ...


@dataclass(frozen=True, repr=False)
class LocalCachePlanIntent:
    """Locally known inputs; route and capabilities are intentionally absent.

    A declared model is carried verbatim in ``producer.model`` (``declared:<label>``).
    Context preparation is unavailable at this boundary, even with a snapshot pin.
    A non-native glossary and post-processing stage belong to the current intent,
    since the raw cache identity does not record them.
    """

    source_locale: str
    target_locale: str
    producer: ProducerIdentity
    chunk_limit: int
    glossary_ref: str | None = None
    glossary_native: bool = False
    policy_ref: str | None = None
    context_ref: str | None = None
    stages: tuple[TextStage, ...] = LINGUISTIC_STAGES
    configuration_revision: OpaqueSemanticRevision | None = None
    prompt_strategy: str = "none"
    prompt_revision: OpaqueSemanticRevision | None = None
    context_strategy: str | None = None
    context_revision: OpaqueSemanticRevision | None = None
    terminology_revision: OpaqueSemanticRevision | None = None
    generation_parameters: tuple[tuple[str, CanonicalValue], ...] = ()
    segmentation_revision: OpaqueSemanticRevision = SEGMENTATION_REVISION
    chunking_revision: OpaqueSemanticRevision = CHUNKING_REVISION
    protection_revision: OpaqueSemanticRevision = PROTECTION_REVISION
    normalization_revision: OpaqueSemanticRevision = NORMALIZATION_REVISION
    pipeline_revision: OpaqueSemanticRevision = PIPELINE_SEMANTIC_REVISION
    format_constraints: tuple[tuple[str, CanonicalValue], ...] = ()

    def __post_init__(self) -> None:
        ProviderRoute(self.source_locale, self.target_locale)  # Pure locale validation only.
        if type(self.producer) is not ProducerIdentity or type(self.chunk_limit) is not int or self.chunk_limit < 1:
            raise CacheIdentityError("invalid_local_cache_intent")
        if (type(self.glossary_native) is not bool or self.glossary_native and self.glossary_ref is None
                or any(not valid_reference(ref) for ref in (self.glossary_ref, self.policy_ref, self.context_ref))):
            raise CacheIdentityError("invalid_local_cache_intent")
        if (type(self.stages) is not tuple or any(type(stage) is not TextStage for stage in self.stages)
                or tuple(stage.stage_id for stage in self.stages) != tuple(stage.stage_id for stage in LINGUISTIC_STAGES)):
            raise CacheIdentityError("invalid_local_cache_intent")
        if self.context_strategy is None:
            object.__setattr__(self, "context_strategy", "none" if self.context_ref is None else "configured_context")
        if (not isinstance(self.context_strategy, str) or not self.context_strategy
                or self.context_strategy == "none" and (self.context_ref is not None or self.context_revision is not None)):
            raise CacheIdentityError("invalid_context_presence")
        if not self.glossary_native and self.terminology_revision is not None:
            raise CacheIdentityError("invalid_terminology_presence")

    def __repr__(self) -> str:
        return "LocalCachePlanIntent(<redacted>)"

    def _projection(self) -> dict[str, object]:
        """Only the dimensions represented by Knowledge's semantic operation."""
        return {name: getattr(self, name) for name in (
            "pipeline_revision", "prompt_strategy", "prompt_revision", "context_strategy", "context_revision",
            "terminology_revision", "generation_parameters", "segmentation_revision", "chunk_limit",
            "chunking_revision", "protection_revision", "normalization_revision", "format_constraints",
        )} | {"stage_versions": tuple((stage.stage_id, stage.version) for stage in self.stages
                                     if stage.stage_id in RAW_CACHE_STAGE_IDS)}

    def _matches(self, operation: SemanticOperationV2) -> bool:
        if (operation.operation != "translate" or operation.source_locale != self.source_locale
                or operation.target_locale != self.target_locale):
            return False
        if any(hop.producer != self.producer or hop.configuration_revision != self.configuration_revision
               for hop in operation.route_hops):
            return False
        if (self.policy_ref is not None and (self.configuration_revision is None
                                            or self.configuration_revision.snapshot_reference != self.policy_ref)
                or self.glossary_native and (self.terminology_revision is None
                                            or self.terminology_revision.snapshot_reference != self.glossary_ref)):
            return False
        # Knowledge validates and normalizes mappings; Translation does not parse
        # canonical bytes or create route/capability evidence from local options.
        return replace(operation, **self._projection()) == operation


class LocalCachePlanState(StrEnum):
    MISS = "miss"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, repr=False)
class LocalCachePlanResolution:
    state: LocalCachePlanState
    key: ExactEntryKey | None = None
    plan: ResolvedTranslationPlan | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (type(self.state) is not LocalCachePlanState
                or (self.state is LocalCachePlanState.RESOLVED) != (self.key is not None and self.plan is not None)
                or self.state is not LocalCachePlanState.RESOLVED and (self.key is not None or self.plan is not None)
                or (self.state is LocalCachePlanState.UNSUPPORTED) != (self.reason is not None)):
            raise CacheIdentityError("invalid_local_cache_resolution")
        if self.key is not None and (type(self.key) is not ExactEntryKey or type(self.plan) is not ResolvedTranslationPlan
                                     or self.plan.semantic_fingerprint != self.key.semantic_fingerprint):
            raise CacheIdentityError("invalid_local_cache_resolution")
        if self.reason is not None and self.reason not in {"offline_context_preparation_required", "cache_route_not_supported"}:
            raise CacheIdentityError("invalid_local_cache_resolution")

    def __repr__(self) -> str:
        return f"LocalCachePlanResolution(state={self.state.value!r}, reason={self.reason!r})"


def _reconstruct(operation: SemanticOperationV2, intent: LocalCachePlanIntent) -> ResolvedTranslationPlan:
    hop = operation.route_hops[0]
    revision = hop.configuration_revision
    context = operation.context_revision
    terminology = operation.terminology_revision
    route = ProviderRoute(operation.source_locale, operation.target_locale,
                          operation.route_hops[0].target_locale if len(operation.route_hops) == 2 else None)
    fields = {name: getattr(operation, name) for name in intent._projection() if name != "stage_versions"}
    return ResolvedTranslationPlan(
        hop.producer, route, hop.capability_fingerprint, **fields,
        configuration_revision=revision, policy_ref=intent.policy_ref,
        context_ref=None if context is None else context.snapshot_reference,
        glossary_native=terminology is not None,
        glossary_ref=intent.glossary_ref if terminology is None else terminology.snapshot_reference,
        stages=intent.stages, route_hops=operation.route_hops,
    )


class LocalCachePlanResolver:
    """Resolve independently per source unit; integrity/resource failures raise CacheIdentityError."""

    def __init__(self, records: CanonicalCacheRecordsPort, scope: AuthorizedCacheScope,
                 intent: LocalCachePlanIntent) -> None:
        if type(scope) is not AuthorizedCacheScope or type(intent) is not LocalCachePlanIntent:
            raise CacheIdentityError("invalid_identity_resolution")
        self._records, self._scope, self._intent = records, scope, intent

    def resolve(self, source_text: str) -> LocalCachePlanResolution:
        intent = self._intent
        if intent.context_strategy != "none":
            return LocalCachePlanResolution(LocalCachePlanState.UNSUPPORTED, reason="offline_context_preparation_required")
        digest = source_content_digest(source_text)
        records = self._records.canonical_records(self._scope, TRANSLATION_STAGE_AGGREGATE, digest,
                                                  limit=MAX_CACHE_ONLY_CANDIDATES)
        if type(records) is not tuple or any(type(record) is not CanonicalCacheRecord for record in records):
            raise CacheIdentityError("invalid_compatible_candidate")
        selection = resolve_compatible_identities((record.key for record in records), scope=self._scope,
                                                  value_kind=TRANSLATION_STAGE_AGGREGATE, source_digest=digest)
        if selection.state is CacheIdentityResolutionState.CORRUPT:
            raise CacheIdentityError("cache_entry_corrupt")
        compatible = []
        for record in records:
            operation = semantic_operation_from_canonical(record.canonical_identity)
            if record.source_text != source_text or operation.fingerprint != record.key.semantic_fingerprint:
                raise CacheIdentityError("cache_entry_corrupt")
            if intent._matches(operation):
                compatible.append((record.key, operation))
        selection = resolve_compatible_identities((key for key, _ in compatible), scope=self._scope,
                                                  value_kind=TRANSLATION_STAGE_AGGREGATE, source_digest=digest)
        if selection.state is not CacheIdentityResolutionState.RESOLVED:
            return LocalCachePlanResolution(LocalCachePlanState(selection.state.value))
        key, operation = compatible[0]
        if len(operation.route_hops) > 2:
            return LocalCachePlanResolution(LocalCachePlanState.UNSUPPORTED, reason="cache_route_not_supported")
        try:
            plan = _reconstruct(operation, intent)
        except TranslationPlanError:
            return LocalCachePlanResolution(LocalCachePlanState.UNSUPPORTED, reason="cache_route_not_supported")
        if plan.semantic_fingerprint != key.semantic_fingerprint:
            raise CacheIdentityError("cache_entry_corrupt")
        return LocalCachePlanResolution(LocalCachePlanState.RESOLVED, key, plan)
