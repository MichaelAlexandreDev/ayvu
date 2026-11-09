"""The immutable decision of how a document will be translated, made before any lookup.

Pure values. Fingerprints split at the glossary: everything that produced the raw text is on
one side and post-processing on the other, which is what makes glossary-only replay safe.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .cache_identity import (
    CACHE_IDENTITY_VERSION,
    CacheEligibility,
    CacheEligibilityState,
    CacheIdentityError,
    CacheIneligibleReason,
    CanonicalValue,
    OpaqueSemanticRevision,
    SemanticOperationV2,
    SemanticRouteHop,
)
from .provenance import ProducerIdentity
from .provider import ProviderRoute, valid_reference
from .provider_capabilities import (
    NegotiationDecision, ProviderCapabilitySnapshot, ProviderRequirements, capability_fingerprint, negotiate,
)

PLAN_SCHEMA_VERSION = 1


class TranslationPlanError(ValueError):
    """A bounded error: messages never include caller data."""


@dataclass(frozen=True)
class TextStage:
    stage_id: str
    version: int = 1


# The order is the contract: a text that leaves early never reaches the stages after it.
LINGUISTIC_STAGES = (
    TextStage("unit_whitespace"),
    TextStage("exact_reuse"),
    TextStage("legacy_fuzzy_reuse"),
    TextStage("execution_gates"),
    TextStage("protection"),
    TextStage("provider_chunks"),
    TextStage("placeholder_restoration"),
    TextStage("raw_cache_write"),
    TextStage("post_processing"),
)
_POST_PROCESSING_STAGES = frozenset({"post_processing"})
RAW_CACHE_STAGE_IDS = tuple(stage.stage_id for stage in LINGUISTIC_STAGES
                            if stage.stage_id not in _POST_PROCESSING_STAGES)
PIPELINE_SEMANTIC_REVISION = OpaqueSemanticRevision("ayvu", "pipeline", 2, "translation-pipeline-v2")
SEGMENTATION_REVISION = OpaqueSemanticRevision("ayvu", "segmentation", 1, "translation-unit-v1")
CHUNKING_REVISION = OpaqueSemanticRevision("ayvu", "chunking", 1, "provider-chunks-v1")
PROTECTION_REVISION = OpaqueSemanticRevision("ayvu", "protection", 1, "protected-input-v1")
NORMALIZATION_REVISION = OpaqueSemanticRevision("ayvu", "normalization", 1, "outer-whitespace-v1")


def _digest(kind: str, value: object) -> str:
    text = json.dumps(["ayvu.translation_plan", PLAN_SCHEMA_VERSION, kind, value], ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('ascii')).hexdigest()}"


@dataclass(frozen=True, repr=False)
class ResolvedTranslationPlan:
    """Build it with ``resolve_plan``. Any change after a lookup means a new plan, so a new identity."""

    producer: ProducerIdentity
    route: ProviderRoute
    capabilities: str
    chunk_limit: int
    glossary_ref: str | None = None
    glossary_native: bool = False
    policy_ref: str | None = None
    context_ref: str | None = None
    stages: tuple[TextStage, ...] = LINGUISTIC_STAGES
    schema_version: int = PLAN_SCHEMA_VERSION
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
    route_hops: tuple[SemanticRouteHop, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PLAN_SCHEMA_VERSION:
            raise TranslationPlanError("unsupported_plan_schema")
        if type(self.producer) is not ProducerIdentity or type(self.route) is not ProviderRoute:
            raise TranslationPlanError("invalid_plan")
        if not isinstance(self.capabilities, str) or not self.capabilities.startswith("capfp:"):
            raise TranslationPlanError("invalid_plan")
        if type(self.chunk_limit) is not int or self.chunk_limit < 1:
            raise TranslationPlanError("invalid_chunk_limit")
        if type(self.glossary_native) is not bool or (self.glossary_native and self.glossary_ref is None):
            raise TranslationPlanError("invalid_plan")
        for reference in (self.glossary_ref, self.policy_ref, self.context_ref):
            if not valid_reference(reference):
                raise TranslationPlanError("invalid_reference")
        revisions = (self.configuration_revision, self.prompt_revision, self.context_revision,
                     self.terminology_revision, self.segmentation_revision, self.chunking_revision,
                     self.protection_revision, self.normalization_revision, self.pipeline_revision)
        if any(item is not None and type(item) is not OpaqueSemanticRevision for item in revisions):
            raise TranslationPlanError("invalid_semantic_revision")
        if not isinstance(self.prompt_strategy, str) or not self.prompt_strategy:
            raise TranslationPlanError("invalid_prompt_strategy")
        if self.prompt_strategy == "none" and self.prompt_revision is not None:
            raise TranslationPlanError("invalid_prompt_presence")
        context_strategy = self.context_strategy
        if context_strategy is None:
            context_strategy = "none" if self.context_ref is None else "configured_context"
            object.__setattr__(self, "context_strategy", context_strategy)
        if not isinstance(context_strategy, str) or not context_strategy:
            raise TranslationPlanError("invalid_context_strategy")
        if context_strategy == "none" and (self.context_ref is not None or self.context_revision is not None):
            raise TranslationPlanError("invalid_context_presence")
        if context_strategy != "none" and self.context_ref is None:
            raise TranslationPlanError("invalid_context_presence")
        if not self.glossary_native and self.terminology_revision is not None:
            raise TranslationPlanError("invalid_terminology_presence")
        for collection in (self.generation_parameters, self.format_constraints):
            if type(collection) is not tuple or any(type(pair) is not tuple or len(pair) != 2
                                                    or not isinstance(pair[0], str)
                                                    or type(pair[1]) is not CanonicalValue for pair in collection):
                raise TranslationPlanError("invalid_semantic_inputs")
        if self.route_hops is None:
            locales = ((self.route.source, self.route.target) if self.route.intermediate is None else
                       (self.route.source, self.route.intermediate, self.route.target))
            hops = tuple(SemanticRouteHop(left, right, self.producer, self.capabilities, self.configuration_revision)
                         for left, right in zip(locales, locales[1:]))
            object.__setattr__(self, "route_hops", hops)
        elif (type(self.route_hops) is not tuple or not self.route_hops
              or any(type(item) is not SemanticRouteHop for item in self.route_hops)):
            raise TranslationPlanError("invalid_route_hops")
        route_locales = ((self.route.source, self.route.target) if self.route.intermediate is None else
                         (self.route.source, self.route.intermediate, self.route.target))
        if (self.route_hops[0].source_locale, *(hop.target_locale for hop in self.route_hops)) != route_locales:
            raise TranslationPlanError("invalid_route_hops")
        # This plan has one resolved provider instance for the complete route. A richer
        # per-hop route can only enter v2 once the execution boundary resolves each hop.
        if any((hop.producer, hop.capability_fingerprint, hop.configuration_revision)
               != (self.producer, self.capabilities, self.configuration_revision) for hop in self.route_hops):
            raise TranslationPlanError("invalid_route_hops")
        if (type(self.stages) is not tuple or not all(type(item) is TextStage for item in self.stages)
                or [item.stage_id for item in self.stages] != [item.stage_id for item in LINGUISTIC_STAGES]):
            raise TranslationPlanError("invalid_stages")

    def __repr__(self) -> str:
        return "ResolvedTranslationPlan()"

    @property
    def cache_eligibility(self) -> CacheEligibility:
        """Unknown critical identities and unpinned low-entropy inputs fail closed."""
        for hop in self.route_hops or ():
            producer = hop.producer
            if producer.provider == "unknown":
                return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.UNKNOWN_PROVIDER)
            if producer.model == "unknown":
                return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.UNKNOWN_MODEL)
            if producer.adapter == "unknown":
                return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.UNKNOWN_ADAPTER)
            if producer.pipeline == "unknown":
                return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.UNKNOWN_PRODUCER_PIPELINE)
        if self.policy_ref is not None and self.configuration_revision is None:
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.MISSING_CONFIGURATION_REVISION)
        if self.context_strategy != "none" and self.context_revision is None:
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.MISSING_CONTEXT_REVISION)
        if self.prompt_strategy != "none" and self.prompt_revision is None:
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.MISSING_PROMPT_REVISION)
        if self.glossary_native and self.terminology_revision is None:
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.MISSING_TERMINOLOGY_REVISION)
        low_entropy_revisions = (self.configuration_revision, self.context_revision, self.prompt_revision,
                                 self.terminology_revision,
                                 *(hop.configuration_revision for hop in self.route_hops or ()))
        if any(item is not None and item.snapshot_reference is None for item in low_entropy_revisions):
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.MISSING_SEMANTIC_SNAPSHOT)
        if ((self.policy_ref is not None and self.configuration_revision is not None
             and self.configuration_revision.snapshot_reference != self.policy_ref)
                or (self.context_ref is not None and self.context_revision is not None
                    and self.context_revision.snapshot_reference != self.context_ref)
                or (self.glossary_native and self.glossary_ref is not None and self.terminology_revision is not None
                    and self.terminology_revision.snapshot_reference != self.glossary_ref)):
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.REVISION_SNAPSHOT_MISMATCH)
        try:
            self._build_semantic_operation()
        except CacheIdentityError:
            return CacheEligibility(CacheEligibilityState.INELIGIBLE, CacheIneligibleReason.INVALID_SEMANTIC_INPUT)
        return CacheEligibility(CacheEligibilityState.ELIGIBLE)

    def _build_semantic_operation(self) -> SemanticOperationV2:
        return SemanticOperationV2(
            operation="translate",
            source_locale=self.route.source,
            target_locale=self.route.target,
            route_hops=self.route_hops or (),
            pipeline_revision=self.pipeline_revision,
            stage_versions=tuple((item.stage_id, item.version) for item in self.stages
                                 if item.stage_id in RAW_CACHE_STAGE_IDS),
            prompt_strategy=self.prompt_strategy,
            prompt_revision=self.prompt_revision,
            context_strategy=self.context_strategy or "none",
            context_revision=self.context_revision,
            terminology_revision=self.terminology_revision if self.glossary_native else None,
            generation_parameters=self.generation_parameters,
            segmentation_revision=self.segmentation_revision,
            chunk_limit=self.chunk_limit,
            chunking_revision=self.chunking_revision,
            protection_revision=self.protection_revision,
            normalization_revision=self.normalization_revision,
            format_constraints=self.format_constraints,
            version=CACHE_IDENTITY_VERSION,
        )

    @property
    def semantic_operation(self) -> SemanticOperationV2 | None:
        if self.cache_eligibility.state is not CacheEligibilityState.ELIGIBLE:
            return None
        return self._build_semantic_operation()

    @property
    def semantic_fingerprint(self) -> str | None:
        operation = self.semantic_operation
        return None if operation is None else operation.fingerprint

    @property
    def raw_fingerprint(self) -> str:
        """Everything that produced the raw text. A glossary counts only if the provider received it."""
        return _digest("raw", {
            "capabilities": self.capabilities,
            "chunk_limit": self.chunk_limit,
            "context_ref": self.context_ref,
            "native_glossary_ref": self.glossary_ref if self.glossary_native else None,
            "policy_ref": self.policy_ref,
            "producer": [self.producer.provider, self.producer.model, self.producer.adapter, self.producer.pipeline],
            "route": [self.route.source, self.route.intermediate, self.route.target],
            "stages": [[item.stage_id, item.version] for item in self.stages
                       if item.stage_id not in _POST_PROCESSING_STAGES],
        })

    @property
    def post_processing_fingerprint(self) -> str:
        return _digest("post_processing", {
            "glossary_ref": self.glossary_ref,
            "stages": [[item.stage_id, item.version] for item in self.stages
                       if item.stage_id in _POST_PROCESSING_STAGES],
        })


def resolve_plan(snapshot: ProviderCapabilitySnapshot, requirements: ProviderRequirements, producer: ProducerIdentity,
                 route: ProviderRoute, *, chunk_limit: int, glossary_ref: str | None = None,
                 glossary_native: bool = False, policy_ref: str | None = None,
                 context_ref: str | None = None, configuration_revision: OpaqueSemanticRevision | None = None,
                 prompt_strategy: str = "none", prompt_revision: OpaqueSemanticRevision | None = None,
                 context_strategy: str | None = None, context_revision: OpaqueSemanticRevision | None = None,
                 terminology_revision: OpaqueSemanticRevision | None = None,
                 generation_parameters: tuple[tuple[str, CanonicalValue], ...] = (),
                 segmentation_revision: OpaqueSemanticRevision = SEGMENTATION_REVISION,
                 chunking_revision: OpaqueSemanticRevision = CHUNKING_REVISION,
                 protection_revision: OpaqueSemanticRevision = PROTECTION_REVISION,
                 normalization_revision: OpaqueSemanticRevision = NORMALIZATION_REVISION,
                 pipeline_revision: OpaqueSemanticRevision = PIPELINE_SEMANTIC_REVISION,
                 format_constraints: tuple[tuple[str, CanonicalValue], ...] = (),
                 route_hops: tuple[SemanticRouteHop, ...] | None = None) -> ResolvedTranslationPlan:
    """Pure: discovery already happened. A requirement that is not plainly accepted yields no plan."""
    if negotiate(snapshot, requirements).decision is not NegotiationDecision.ACCEPT:
        raise TranslationPlanError("requirements_not_accepted")
    return ResolvedTranslationPlan(
        producer, route, capability_fingerprint(snapshot), chunk_limit, glossary_ref, glossary_native, policy_ref,
        context_ref, configuration_revision=configuration_revision, prompt_strategy=prompt_strategy,
        prompt_revision=prompt_revision, context_strategy=context_strategy, context_revision=context_revision,
        terminology_revision=terminology_revision, generation_parameters=generation_parameters,
        segmentation_revision=segmentation_revision, chunking_revision=chunking_revision,
        protection_revision=protection_revision, normalization_revision=normalization_revision,
        pipeline_revision=pipeline_revision, format_constraints=format_constraints, route_hops=route_hops,
    )
