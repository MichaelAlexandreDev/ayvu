"""The provider the rich runtime translates with, and the plan that names it.

A binding resolves the plan of one run and creates the provider each worker uses, so the
runtime itself never constructs a concrete provider. LibreTranslate is the default binding;
``composition.builders.llm`` binds the LLM core.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable

from ...knowledge.glossary import effective_glossary_reference
from ...provenance import ProducerIdentity, declared_model
from ...providers import Capability, ProviderRequirements, ProviderRoute
from ...providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ...providers.libretranslate.client import LibreTranslateTranslator, RoutedTranslator
from ...translation import resolve_plan


def _base(translator: object) -> object:
    return translator._base if isinstance(translator, RoutedTranslator) else translator


def selected_model(models: dict[str, str], translator_name: str) -> str | None:
    """Selection belongs here, never in the presenter, engine or domain."""
    name = translator_name.strip().lower()
    if name not in models:
        return None
    label = models[name]
    declared_model(label)
    return label


class LibreTranslateBinding:
    """The prepared LibreTranslate translator, as the rich runtime has always used it."""

    def __init__(self, label: str, clock: Callable[[], datetime]) -> None:
        self.producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label))
        self._clock = clock

    def __repr__(self) -> str:
        return "LibreTranslateBinding()"

    def plan(self, prepared, options):
        translator = prepared.translator
        route = translator.route if isinstance(translator, RoutedTranslator) else None
        return resolve_plan(libretranslate_snapshot(_base(translator), self._clock()),
            ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT)), self.producer,
            ProviderRoute(options.source, options.target, None if route is None else route.intermediate),
            chunk_limit=options.chunk_limit, glossary_ref=effective_glossary_reference(prepared.glossary))

    def provider(self, translator, producer: ProducerIdentity, glossary: object = None) -> LibreTranslateProvider:
        """The glossary is not sent to LibreTranslate; the pipeline applies it after translation."""
        base = _base(translator)
        return LibreTranslateProvider(translator, producer=producer, clock=self._clock,
            cleanup=base.close if isinstance(base, LibreTranslateTranslator) else None)
