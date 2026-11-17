"""The provider the rich runtime translates with, and the plan that names it.

A binding resolves the plan of one run and creates the provider each worker uses, so the
runtime itself never constructs a concrete provider. LibreTranslate is the default binding;
``composition.builders.llm`` binds the LLM core, and Ollama is its first model translator.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable

from ...knowledge.glossary import effective_glossary_reference
from ...provenance import ProducerIdentity, ProvenanceValidationError, declared_model, valid_observed_model_name
from ...providers import Capability, ProviderRequirements, ProviderRoute
from ...providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ...providers.libretranslate.client import LibreTranslateTranslator, RoutedTranslator
from ...translation import resolve_plan
from .ollama import OLLAMA, ollama_preflight


def _base(translator: object) -> object:
    return translator._base if isinstance(translator, RoutedTranslator) else translator


class IdentitySource:
    """Where a translator's model identity comes from: one answer for every admission and selection rule."""

    DECLARED_LABEL = "declared_label"  # LibreTranslate: the owner declares it in translator_models
    DISCOVERED_MODEL = "discovered_model"  # Ollama: discovery reports the named model's digest before any work


IDENTITY_SOURCES = {"libretranslate": IdentitySource.DECLARED_LABEL, OLLAMA: IdentitySource.DISCOVERED_MODEL}


def identity_source(translator_name: str) -> str | None:
    """``None`` for a translator Ayvu does not know; validation of the name stays with each translator's preflight."""
    return IDENTITY_SOURCES.get(translator_name.strip().lower())


def discovers_identity(translator_name: str) -> bool:
    """A translator whose identity is discovered needs no declared label: its own preflight finds the model."""
    return identity_source(translator_name) == IdentitySource.DISCOVERED_MODEL


def selected_model(models: dict[str, str], translator_name: str) -> str | None:
    """Selection belongs here, never in the presenter, engine or domain.

    LibreTranslate's entry is a declared label; Ollama's is the name of an installed model.
    """
    name = translator_name.strip().lower()
    if name not in models:
        return None
    label = models[name]
    if discovers_identity(name):
        if not valid_observed_model_name(label):
            raise ProvenanceValidationError("invalid_model_name")
        return label
    declared_model(label)
    return label


def model_selection(models: dict[str, str] | None, translator_name: str, options) -> dict[str, object]:
    """What a model translator's preflight needs beyond LibreTranslate's arguments: its model and the workers."""
    if not discovers_identity(translator_name):
        return {}
    return {"model": selected_model(models or {}, translator_name), "workers": options.workers}


def translation_preflight(libretranslate_preflight: Callable[..., object], book_checks: Callable[..., object],
                          **configuration) -> object:
    """The preflight of the selected translator. Each one refuses what it cannot run before any book work."""
    if discovers_identity(str(configuration.get("translator_name", ""))):
        return ollama_preflight(book_checks=book_checks, **configuration)
    return libretranslate_preflight(**configuration)


def prepare_model_translation(translator_name: str, prepare: Callable[[], object]) -> object | None:
    """A model translator is prepared before its job: its producer names the model's digest, found by discovery."""
    return prepare() if discovers_identity(translator_name) else None


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
