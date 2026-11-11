from __future__ import annotations
import ast
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
import pytest
import ayvu.composition.builders.translate_project as composition
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.knowledge.cache import CacheEligibilityState, CacheIneligibleReason, PutIfAbsentState
from ayvu.knowledge.cache.schema import SchemaState, classify_cache
from ayvu.preferences.config_facade import ConfigStore
from ayvu.preferences.model import AyvuConfig, config_from_preferences, config_to_preferences
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.jobs.events import encode_event
from ayvu.jobs.execution import CancellationSource
from ayvu.preferences.model import GlobalPreferences, PreferencesError, preferences_from_mapping, preferences_to_mapping
from ayvu.provenance.model import ProducerIdentity, ProvenanceValidationError, declared_model
from ayvu.providers.ports import ProviderRoute
from ayvu.providers.capabilities import Capability, ProviderRequirements
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LIBRETRANSLATE_SAFE_POINTS, libretranslate_snapshot
from ayvu.translation.project_contracts import RuntimeContext
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import PIPELINE_SAFE_POINTS, TranslationExecutionError
from ayvu.translation.plan import ResolvedTranslationPlan, resolve_plan
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from tests.support.cache_repository_v2_support import entry
from tests.support.cli_translate_project_support import Session
from tests.support.translation_cache_v2_support import Env, NOW, document
from ayvu.kernel import LanguagePair, TranslationMemoryOptions, TranslationOptions, OutputPlan, JobFailure, JobFailureFamily
from ayvu.translation import BookRefused

PAIR = LanguagePair(source="en", target="pt")


def runtime(tmp_path, *, options=None, prepare=None, label="local-v1"):
    return composition.RichTranslationRuntime(tmp_path / "source.epub", options or TranslationOptions(language_pair=PAIR),
        prepare or (lambda: composition.LegacyResources(None)), tmp_path / "cache.sqlite", label,
        tmp_path / "project.sqlite", tmp_path / "backups", tmp_path / "artifacts", document().project_id, clock=lambda: NOW)


def prepared_runtime(tmp_path, monkeypatch, *, label="local-v1", session=None, glossary=None):
    """Unit harness: real document pipeline/cache/HTTP adapter, in-memory artifact and provenance ports."""
    env = Env()
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session or Session()
    subject = runtime(tmp_path, label=label, prepare=lambda: composition.LegacyResources(translator, glossary))
    assert subject.preflight() is None
    monkeypatch.setattr(composition, "open_artifact_service", lambda *a, **kw: env.artifacts)
    monkeypatch.setattr(composition, "open_provenance_repository", lambda *a, **kw: env.provenance)
    context = RuntimeContext(env.job, CancellationSource((*composition.DOCUMENT_SAFE_POINTS, *LIBRETRANSLATE_SAFE_POINTS)), env.events)
    return subject, context, env, translator.session
