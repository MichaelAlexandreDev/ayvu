"""Shared translation report values and conversion from engine results."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..formats import BookPackage
from ..kernel import TranslationOptions
from ..knowledge.glossary import GlossaryUsage
from ..knowledge.memory import TranslationMemoryMatch
from .engine import EngineResult, TextStats


@dataclass
class HtmlTranslationStats:
    translated: int = 0
    from_cache: int = 0
    from_memory: int = 0
    memory_suggestions: int = 0
    skipped: int = 0
    alt_translated: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)
    missing_texts: list[str] = field(default_factory=list)
    memory_suggestion_texts: list[str] = field(default_factory=list)
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)


@dataclass(frozen=True)
class TextTranslationResult:
    text: str
    from_cache: bool = False
    from_memory: bool = False
    missing: bool = False
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)
    memory_suggestion: TranslationMemoryMatch | None = None


@dataclass
class TranslationReport:
    chapters_processed: int = 0
    texts_translated: int = 0
    texts_from_cache: int = 0
    texts_from_memory: int = 0
    memory_suggestions: int = 0
    texts_skipped: int = 0
    texts_missing: int = 0
    alt_texts_translated: int = 0
    errors: list[str] = field(default_factory=list)
    missing_texts: list[str] = field(default_factory=list)
    memory_suggestion_texts: list[str] = field(default_factory=list)
    glossary_terms_configured: int = 0
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)
    output_path: Path | None = None
    output_written: bool = True
    review_output_path: Path | None = None
    input_path: Path | None = None
    detected_language: str | None = None
    target_language: str | None = None

    def record_error(self, message: str) -> None:
        self.errors.append(message)

    def record_chapter(self, stats: HtmlTranslationStats) -> None:
        self.texts_translated += stats.translated
        self.texts_from_cache += stats.from_cache
        self.texts_from_memory += stats.from_memory
        self.memory_suggestions += stats.memory_suggestions
        self.texts_skipped += stats.skipped
        self.texts_missing += stats.missing
        self.alt_texts_translated += stats.alt_translated
        self.errors.extend(stats.errors)
        self.missing_texts.extend(stats.missing_texts)
        self.memory_suggestion_texts.extend(stats.memory_suggestion_texts)
        self.glossary_usage.merge(stats.glossary_usage)
        self.chapters_processed += 1

    def record_text(self, result: TextTranslationResult) -> None:
        if result.memory_suggestion is not None:
            self.memory_suggestions += 1
            self.memory_suggestion_texts.append(result.memory_suggestion.original)
        if result.from_memory:
            self.texts_from_memory += 1
        elif result.missing:
            self.texts_missing += 1
            self.missing_texts.append(result.text)
        elif result.from_cache:
            self.texts_from_cache += 1
        else:
            self.texts_translated += 1
        self.glossary_usage.merge(result.glossary_usage)


def to_html_stats(stats: TextStats) -> HtmlTranslationStats:
    converted = HtmlTranslationStats(
        translated=stats.translated, from_cache=stats.from_cache, from_memory=stats.from_memory,
        memory_suggestions=stats.memory_suggestions, alt_translated=stats.alt_translated, missing=stats.missing,
        errors=list(stats.errors), missing_texts=list(stats.missing_texts),
        memory_suggestion_texts=list(stats.memory_suggestion_texts))
    for usage in stats.glossary_usages:
        converted.glossary_usage.merge(usage)
    return converted


def to_report(result: EngineResult, source_path: Path, destination_path: Path, package: BookPackage,
               options: TranslationOptions, glossary: object | None) -> TranslationReport:
    report = TranslationReport(
        output_path=destination_path, input_path=source_path, detected_language=package.language, target_language=options.target,
        glossary_terms_configured=len(glossary.entries) if glossary else 0, output_written=result.output_written,
        chapters_processed=result.chapters_processed)
    stats = to_html_stats(result.stats)
    report.texts_translated, report.texts_from_cache = stats.translated, stats.from_cache
    report.texts_from_memory, report.memory_suggestions = stats.from_memory, stats.memory_suggestions
    report.texts_missing, report.alt_texts_translated = stats.missing, stats.alt_translated
    report.errors, report.missing_texts = stats.errors, stats.missing_texts
    report.memory_suggestion_texts = stats.memory_suggestion_texts
    report.glossary_usage = stats.glossary_usage
    if glossary and result.output_written:
        report.glossary_usage.finalize_required_terms(glossary)
    return report
