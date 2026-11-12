"""Stable composition capabilities and their existing implementation seams.

Inventory numbers are traceability, never persisted capability identities. Binding
paths describe existing selections/workflows; importing this catalog acquires nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Feature(str, Enum):
    TRANSLATE_EPUB = "translate_epub"
    PREVIEW = "preview"
    RESUME_TRANSLATION = "resume_translation"
    PENDING_TRANSLATIONS = "pending_translations"
    SIMPLE_GLOSSARY = "simple_glossary"
    GUIDED_MENU = "guided_menu"
    FIRST_USE_SETUP = "first_use_setup"
    SETTINGS = "settings"
    OUTPUT_DESTINATION = "output_destination"
    TRANSLATION_SUMMARY = "translation_summary"
    TEST_TRANSLATOR = "test_translator"
    FULL_REPORT = "full_report"
    PERFORMANCE_PROFILE_SELECTION = "performance_profile_selection"
    BATCH_TRANSLATION = "batch_translation"
    CHAPTER_SELECTION = "chapter_selection"
    TRANSLATE_METADATA = "translate_metadata"
    TRANSLATE_ALT_TEXT = "translate_alt_text"
    TRANSLATION_MEMORY = "translation_memory"
    APPROVED_MEMORY = "approved_memory"
    GLOSSARY_CATALOG = "glossary_catalog"
    TRANSLATION_CONTEXT = "translation_context"
    REVIEW_EXPORT = "review_export"
    REVIEW_APPLY = "review_apply"
    TRANSLATION_PROFILES = "translation_profiles"
    LIBRARY = "library"
    INSPECT_EPUB = "inspect_epub"
    EXTRACT_MARKDOWN = "extract_markdown"
    DRY_RUN = "dry_run"
    FAIL_FAST = "fail_fast"
    WORKER_SELECTION = "worker_selection"
    HTTP_CONTROL_SELECTION = "http_control_selection"
    CHUNK_LIMIT_SELECTION = "chunk_limit_selection"
    LIST_LANGUAGES = "list_languages"
    CACHE_ONLY = "cache_only"
    CACHE_ADMINISTRATION = "cache_administration"
    CACHE_SCOPES = "cache_scopes"
    DEVELOPER_MODE = "developer_mode"
    AI_ASSISTANCE = "ai_assistance"
    AI_TRANSLATION_PROVIDERS = "ai_translation_providers"


@dataclass(frozen=True)
class FeatureDefinition:
    id: Feature
    inventory_ids: tuple[str, ...]
    requires: frozenset[Feature]
    binding: str | None


def _definition(feature, inventory, binding, *requires):
    return FeatureDefinition(feature, (inventory,), frozenset(requires), binding)


# Internal capabilities bind to their owning workflow/value, not invented commands.
FEATURES = (
    _definition(Feature.TRANSLATE_EPUB, "1.1", "composition.cli.prepare_translation"),
    _definition(Feature.PREVIEW, "1.10", "interfaces.cli.preview._run_preview", Feature.TRANSLATE_EPUB),
    _definition(Feature.RESUME_TRANSLATION, "2.1", "interfaces.cli.commands.resume.resume", Feature.TRANSLATE_EPUB),
    _definition(Feature.PENDING_TRANSLATIONS, "2.2", "composition.cli.interrupted_translations", Feature.RESUME_TRANSLATION),
    _definition(Feature.SIMPLE_GLOSSARY, "4.7", "knowledge.glossary.rules.Glossary", Feature.TRANSLATE_EPUB),
    _definition(Feature.GUIDED_MENU, "7.1", "interfaces.cli.guided.menu._run_guided_main_flow"),
    _definition(Feature.FIRST_USE_SETUP, "7.2", "interfaces.cli.guided.menu._init_default_language_config", Feature.GUIDED_MENU),
    _definition(Feature.SETTINGS, "7.3", "interfaces.cli.guided.preferences._run_guided_settings", Feature.GUIDED_MENU),
    _definition(Feature.OUTPUT_DESTINATION, "1.7", "kernel.values.OutputPlan", Feature.TRANSLATE_EPUB),
    _definition(Feature.TRANSLATION_SUMMARY, "1.12", "interfaces.cli.report._print_report", Feature.TRANSLATE_EPUB),
    _definition(Feature.TEST_TRANSLATOR, "3.6", "interfaces.cli.commands.translator.test_translator"),
    _definition(Feature.FULL_REPORT, "1.12", "composition.cli.publish_cli_report", Feature.TRANSLATION_SUMMARY),
    _definition(Feature.PERFORMANCE_PROFILE_SELECTION, "3.1", "kernel.values.PerformanceProfile", Feature.TRANSLATE_EPUB),
    _definition(Feature.BATCH_TRANSLATION, "1.3", "composition.cli.translate_batch", Feature.TRANSLATE_EPUB),
    _definition(Feature.CHAPTER_SELECTION, "1.4", "composition.cli.resolve_chapter_selection", Feature.TRANSLATE_EPUB),
    _definition(Feature.TRANSLATE_METADATA, "1.5", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.TRANSLATE_ALT_TEXT, "1.6", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.TRANSLATION_MEMORY, "4.5", "translation.stages.memory_retrieval.MemoryRetrieval", Feature.TRANSLATE_EPUB),
    _definition(Feature.APPROVED_MEMORY, "4.6", "knowledge.memory.approved.service.ApprovedMemory", Feature.TRANSLATION_MEMORY),
    _definition(Feature.GLOSSARY_CATALOG, "4.8", "composition.cli.managed_glossary_repository", Feature.SIMPLE_GLOSSARY),
    _definition(Feature.TRANSLATION_CONTEXT, "4.9", "translation.context.execution.ContextExecution", Feature.TRANSLATE_EPUB),
    _definition(Feature.REVIEW_EXPORT, "5.1", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.REVIEW_APPLY, "5.2", "composition.cli.open_review_use_cases", Feature.TRANSLATE_EPUB),
    _definition(Feature.TRANSLATION_PROFILES, "7.4", "projects.store.translation_selections", Feature.TRANSLATE_EPUB),
    _definition(Feature.LIBRARY, "7.5", "composition.cli.library_service", Feature.GUIDED_MENU),
    _definition(Feature.INSPECT_EPUB, "6.1", "formats.use_cases.InspectContent"),
    _definition(Feature.EXTRACT_MARKDOWN, "6.2", "formats.markdown.writer.MarkdownDirectoryWriter"),
    _definition(Feature.DRY_RUN, "1.8", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.FAIL_FAST, "1.9", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.WORKER_SELECTION, "3.2", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.HTTP_CONTROL_SELECTION, "3.3", "composition.cli.cli_provider_session"),
    _definition(Feature.CHUNK_LIMIT_SELECTION, "3.4", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.LIST_LANGUAGES, "3.7", "interfaces.cli.commands.translator.languages"),
    _definition(Feature.CACHE_ONLY, "4.2", "kernel.values.TranslationOptions", Feature.TRANSLATE_EPUB),
    _definition(Feature.CACHE_ADMINISTRATION, "4.3", "composition.cli.cache_admin_services"),
    _definition(Feature.CACHE_SCOPES, "4.4", "knowledge.cache.scope.AuthorizedCacheScope", Feature.CACHE_ADMINISTRATION),
    _definition(Feature.DEVELOPER_MODE, "7.6", "kernel.values.UserMode"),
    _definition(Feature.AI_ASSISTANCE, "8.3", None),
    _definition(Feature.AI_TRANSLATION_PROVIDERS, "8.4", None),
)

# These guarantees are acquired by workflows on demand and cannot be subtracted.
INFRASTRUCTURE_INVENTORY_IDS = ("1.2", "1.11", "2.3", "2.4", "2.5", "3.5", "3.8", "4.1", "6.4", "8.1")


def validate_features(features, *, definitions=FEATURES):
    """Reject unknown, unimplemented or incomplete effective selections."""
    known = frozenset(definition.id for definition in definitions)
    if len(known) != len(definitions):
        raise ValueError("Duplicate feature definition")
    if features - known:
        raise ValueError("Unknown feature id")
    for definition in definitions:
        if definition.requires - known:
            raise ValueError("Unknown feature dependency")
        if definition.id in features:
            if not definition.binding:
                raise ValueError("Effective feature without binding")
            if definition.requires - features:
                raise ValueError("Missing feature dependency")


def validate_presentation(features, execution_preset, report_style):
    expected_style = "full" if Feature.FULL_REPORT in features else "summary"
    if report_style != expected_style:
        raise ValueError("Report style must agree with full_report")
    if execution_preset is not None and Feature.PERFORMANCE_PROFILE_SELECTION in features:
        raise ValueError("Fixed preset cannot allow performance profile selection")
