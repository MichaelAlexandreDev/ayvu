"""Bind the LLM translation core to the rich runtime.

The caller supplies the model adapter's factory, its producer identity and its capability snapshot;
no vendor is named here. Only composition sees both the core and the cache's identity values, so the
core's plain semantics become the plan's prompt revision and generation parameters here, and a
glossary given to the model becomes a terminology revision: it changes the raw output, so it is
semantic identity. The pipeline still applies the glossary after translation, as for LibreTranslate.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from ...knowledge.cache import CanonicalValue, OpaqueSemanticRevision
from ...knowledge.glossary import Glossary, effective_glossary_reference
from ...provenance import ProducerIdentity
from ...providers import Capability, ProviderCapabilitySnapshot, ProviderRequirements, ProviderRoute
from ...providers.llm import (
    PROMPT_REVISION_NAMESPACE, PROMPT_REVISION_OWNER, GlossaryHint, GlossaryRule, LlmSettings, LlmTranslationProvider,
    ModelPort,
)
from ...translation import resolve_plan

LLM_REQUIREMENTS = ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT,
                                         Capability.STRUCTURED_OUTPUT))
GLOSSARY_HINT_REVISION = ("ayvu", "llm_glossary", 1, "glossary-prompt-hint-v1")


def glossary_hints(glossary: object) -> tuple[GlossaryHint, ...]:
    """The active rules, in the glossary's own order. An absent glossary gives no hints."""
    if not glossary:
        return ()
    entries = glossary.entries if isinstance(glossary, Glossary) else Glossary(glossary).entries
    return tuple(GlossaryHint(GlossaryRule(entry.rule), entry.term,
                              entry.translation if entry.rule == GlossaryRule.TRANSLATE else None) for entry in entries)


def llm_plan_inputs(settings: LlmSettings, glossary_ref: str) -> dict[str, object]:
    """The semantic inputs of ``resolve_plan`` for this core: prompt, batch policy, decoding and glossary hints."""
    semantics = settings.semantics()
    return {
        "glossary_ref": glossary_ref,
        "glossary_native": True,
        "terminology_revision": OpaqueSemanticRevision(*GLOSSARY_HINT_REVISION, snapshot_reference=glossary_ref),
        "prompt_strategy": semantics.prompt_strategy,
        "prompt_revision": OpaqueSemanticRevision(PROMPT_REVISION_OWNER, PROMPT_REVISION_NAMESPACE,
                                                  semantics.prompt_version, semantics.prompt_value,
                                                  snapshot_reference=semantics.prompt_snapshot),
        "generation_parameters": tuple((key, CanonicalValue(kind, value))
                                       for key, kind, value in semantics.generation_parameters),
    }


class LlmBinding:
    """A rich-runtime binding over one model adapter. Each worker's provider gets a model from the factory."""

    def __init__(self, model_factory: Callable[[], ModelPort], producer: ProducerIdentity,
                 snapshot: Callable[[datetime], ProviderCapabilitySnapshot], *, clock: Callable[[], datetime],
                 settings: LlmSettings = LlmSettings(), sleep: Callable[[float], None] = time.sleep) -> None:
        self.producer = producer
        self._model_factory, self._snapshot, self._clock = model_factory, snapshot, clock
        self._settings, self._sleep = settings, sleep

    def __repr__(self) -> str:
        return "LlmBinding()"

    def plan(self, prepared, options):
        return resolve_plan(self._snapshot(self._clock()), LLM_REQUIREMENTS, self.producer,
                            ProviderRoute(options.source, options.target), chunk_limit=options.chunk_limit,
                            **llm_plan_inputs(self._settings, effective_glossary_reference(prepared.glossary)))

    def provider(self, translator, producer: ProducerIdentity, glossary: object) -> LlmTranslationProvider:
        return LlmTranslationProvider(self._model_factory(), producer, settings=self._settings,
                                      glossary=glossary_hints(glossary), sleep=self._sleep)
