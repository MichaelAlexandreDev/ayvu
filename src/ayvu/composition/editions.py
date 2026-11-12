"""Immutable product manifests; selection belongs to composition alone.

These declarations do not launch products, enforce quotas or select a runtime.
Planned capabilities have no effective bindings until their implementation lands.
"""
from __future__ import annotations

from dataclasses import dataclass

from .feature_catalog import FEATURES, Feature, validate_features, validate_presentation


@dataclass(frozen=True)
class Edition:
    id: str
    name: str
    interfaces: frozenset[str]
    features: frozenset[Feature]
    planned_features: frozenset[Feature] = frozenset()
    execution_preset: str | None = None
    report_style: str = "full"


CORE_FEATURES = frozenset((
    Feature.TRANSLATE_EPUB, Feature.PREVIEW, Feature.RESUME_TRANSLATION,
    Feature.PENDING_TRANSLATIONS, Feature.SIMPLE_GLOSSARY, Feature.GUIDED_MENU,
    Feature.FIRST_USE_SETUP, Feature.SETTINGS, Feature.OUTPUT_DESTINATION,
    Feature.TRANSLATION_SUMMARY, Feature.TEST_TRANSLATOR,
))
EXTENDED_FEATURES = frozenset((
    Feature.FULL_REPORT, Feature.PERFORMANCE_PROFILE_SELECTION, Feature.BATCH_TRANSLATION,
    Feature.CHAPTER_SELECTION, Feature.TRANSLATE_METADATA, Feature.TRANSLATE_ALT_TEXT,
    Feature.TRANSLATION_MEMORY, Feature.APPROVED_MEMORY, Feature.GLOSSARY_CATALOG,
    Feature.TRANSLATION_CONTEXT, Feature.REVIEW_EXPORT, Feature.REVIEW_APPLY,
    Feature.TRANSLATION_PROFILES, Feature.LIBRARY, Feature.INSPECT_EPUB, Feature.EXTRACT_MARKDOWN,
))
TERMINAL_FEATURES = frozenset((
    Feature.DRY_RUN, Feature.FAIL_FAST, Feature.WORKER_SELECTION,
    Feature.HTTP_CONTROL_SELECTION, Feature.CHUNK_LIMIT_SELECTION, Feature.LIST_LANGUAGES,
    Feature.CACHE_ONLY, Feature.CACHE_ADMINISTRATION, Feature.CACHE_SCOPES, Feature.DEVELOPER_MODE,
))
PLANNED_AI_FEATURES = frozenset((Feature.AI_ASSISTANCE, Feature.AI_TRANSLATION_PROVIDERS))

EDITIONS = (
    Edition("lite", "Lite", frozenset(("desktop",)), CORE_FEATURES,
            execution_preset="low", report_style="summary"),
    Edition("free", "Free", frozenset(("desktop",)), CORE_FEATURES | EXTENDED_FEATURES),
    Edition("ai", "AI", frozenset(("desktop",)), CORE_FEATURES | EXTENDED_FEATURES,
            planned_features=PLANNED_AI_FEATURES),
    Edition("cli_free", "CLI Free", frozenset(("cli",)), CORE_FEATURES | EXTENDED_FEATURES | TERMINAL_FEATURES),
    Edition("cli_pro", "CLI Pro", frozenset(("cli",)), CORE_FEATURES | EXTENDED_FEATURES | TERMINAL_FEATURES,
            planned_features=PLANNED_AI_FEATURES),
)


def validate_edition(edition: Edition) -> None:
    validate_features(edition.features)
    known = frozenset(definition.id for definition in FEATURES)
    if edition.planned_features - known:
        raise ValueError("Unknown planned feature id")
    if edition.features & edition.planned_features:
        raise ValueError("Effective and planned features must be disjoint")
    if not edition.interfaces or edition.interfaces - frozenset(("cli", "desktop")):
        raise ValueError("Unknown or empty product interfaces")
    validate_presentation(edition.features, edition.execution_preset, edition.report_style)


def get_edition(edition_id: str) -> Edition:
    for edition in EDITIONS:
        if edition.id == edition_id:
            validate_edition(edition)
            return edition
    raise ValueError("Unknown edition id")
