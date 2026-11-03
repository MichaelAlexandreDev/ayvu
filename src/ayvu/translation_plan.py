"""The immutable decision of how a document will be translated, made before any lookup.

Pure values. Fingerprints split at the glossary: everything that produced the raw text is on
one side and post-processing on the other, which is what makes glossary-only replay safe.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

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
        if (type(self.stages) is not tuple or not all(type(item) is TextStage for item in self.stages)
                or [item.stage_id for item in self.stages] != [item.stage_id for item in LINGUISTIC_STAGES]):
            raise TranslationPlanError("invalid_stages")

    def __repr__(self) -> str:
        return f"ResolvedTranslationPlan(raw_fingerprint={self.raw_fingerprint!r})"

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
                 context_ref: str | None = None) -> ResolvedTranslationPlan:
    """Pure: discovery already happened. A requirement that is not plainly accepted yields no plan."""
    if negotiate(snapshot, requirements).decision is not NegotiationDecision.ACCEPT:
        raise TranslationPlanError("requirements_not_accepted")
    return ResolvedTranslationPlan(producer, route, capability_fingerprint(snapshot), chunk_limit, glossary_ref,
                                   glossary_native, policy_ref, context_ref)
