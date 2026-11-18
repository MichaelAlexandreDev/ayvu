"""The provider the rich runtime translates with, and the plan that names it.

A binding resolves the plan of one run and creates the provider each worker uses, so the
runtime itself never constructs a concrete provider. LibreTranslate is the default binding;
``composition.builders.llm`` binds the LLM core, Ollama is its first model translator, and a remote
model provider (``composition.builders.remote``) is prepared inside its project's consent scope.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable

from ...knowledge.glossary import effective_glossary_reference
from ...provenance import ProducerIdentity, ProvenanceValidationError, declared_model, valid_observed_model_name
from ...providers import Capability, ProviderRequirements, ProviderRoute
from ...providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ...providers.libretranslate.client import (
    LibreTranslateTranslator, RoutedTranslator, TranslatorError, validate_translator_name,
)
from ...translation import PreflightError, resolve_plan
from .ollama import OLLAMA, ollama_preflight
from .remote import is_remote, remote_preflight
from .remote_consent import consent_scope, project_gate


def _base(translator: object) -> object:
    return translator._base if isinstance(translator, RoutedTranslator) else translator


class IdentitySource:
    """Where a translator's model identity comes from: one answer for every admission and selection rule."""

    DECLARED_LABEL = "declared_label"  # LibreTranslate: the owner declares it in translator_models
    DISCOVERED_MODEL = "discovered_model"  # Ollama: discovery reports the named model's digest before any work
    REMOTE_MODEL = "remote_model"  # a remote provider: the owner names the provider's versioned model id


IDENTITY_SOURCES = {"libretranslate": IdentitySource.DECLARED_LABEL, OLLAMA: IdentitySource.DISCOVERED_MODEL}


def identity_source(translator_name: str) -> str | None:
    """``None`` for a translator Ayvu does not know; validation of the name stays with each translator's preflight."""
    name = translator_name.strip().lower()
    return IDENTITY_SOURCES.get(name) or (IdentitySource.REMOTE_MODEL if is_remote(name) else None)


def discovers_identity(translator_name: str) -> bool:
    """A translator whose identity is discovered needs no declared label: its own preflight finds the model."""
    return identity_source(translator_name) == IdentitySource.DISCOVERED_MODEL


def names_model(translator_name: str) -> bool:
    """A model translator: its entry names a model, not a label, and it is prepared before its job."""
    return identity_source(translator_name) in (IdentitySource.DISCOVERED_MODEL, IdentitySource.REMOTE_MODEL)


def selected_model(models: dict[str, str], translator_name: str) -> str | None:
    """Selection belongs here, never in the presenter, engine or domain.

    LibreTranslate's entry is a declared label; Ollama's is the name of an installed model, and a remote
    provider's its model id.
    """
    name = translator_name.strip().lower()
    if name not in models:
        return None
    label = models[name]
    if names_model(name):
        if not valid_observed_model_name(label):
            raise ProvenanceValidationError("invalid_model_name")
        return label
    declared_model(label)
    return label


def require_model(models, translator_name, *, continuation=None, legacy_resume=False):
    """Admit new work only with an identity, before acquiring any resource.

    A translator whose identity is discovered (Ollama) is admitted here: its own preflight refuses a
    missing model entry or a model that is not installed, before any book work.
    """
    if names_model(translator_name):
        return
    try:
        validate_translator_name(translator_name)
    except TranslatorError as error:
        raise PreflightError("Não foi possível preparar o tradutor.",
                             "Use --translator libretranslate.", detail=str(error)) from None
    if continuation is not None or legacy_resume:
        return
    if selected_model(models, translator_name) is None:
        name = translator_name.strip().lower()
        raise PreflightError(
            "To translate, declare your translator's model label.",
            'Open Settings (option 4) and choose "5. Change translator model label", '
            f"or add translator_models.{name} to config.json.",
        )


def model_selection(models: dict[str, str] | None, translator_name: str, options) -> dict[str, object]:
    """What a model translator's preflight needs beyond LibreTranslate's arguments: its model and the workers."""
    if not names_model(translator_name):
        return {}
    return {"model": selected_model(models or {}, translator_name), "workers": options.workers}


def translation_preflight(libretranslate_preflight: Callable[..., object], book_checks: Callable[..., object],
                          **configuration) -> object:
    """The preflight of the selected translator. Each one refuses what it cannot run before any book work."""
    name = str(configuration.get("translator_name", ""))
    if is_remote(name):
        return remote_preflight(book_checks=book_checks, **configuration)
    if discovers_identity(name):
        return ollama_preflight(book_checks=book_checks, **configuration)
    return libretranslate_preflight(**configuration)


def prepare_model_translation(translator_name: str, prepare: Callable[[], object], book: object = None) -> object | None:
    """A model translator is prepared before its job: its producer names the model's digest, found by discovery.

    A remote provider is prepared inside the consent scope of the book's project; without a project there
    is no scope, and its preflight refuses.
    """
    if is_remote(translator_name):
        gate = project_gate(book)
        if gate is None:
            return prepare()
        with consent_scope(gate):
            return prepare()
    return prepare() if names_model(translator_name) else None


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
