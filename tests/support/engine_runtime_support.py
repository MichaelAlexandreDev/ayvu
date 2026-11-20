"""A test runtime behind the ``TranslationRuntime`` port, for the use cases that run around one.

``TranslateProject`` and the recovery use cases do not care which language work a runtime does; their tests
need one that is small and predictable. This one runs the real translation engine with a unit executor over
the text pipeline, a fake translator and a cache file in the v1 format. It names no producer, so its jobs keep
command version 1: the shape the jobs of the retired v1 runtime keep on disk, which recovery still reads.
The rich runtime has its own tests (``tests/composition/test_rich_runtime_*``).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ayvu.composition.builders.translate_project import LegacyResources
from ayvu.formats.epub import html_unit_template, normalize_translated_text, review_text_from_template, without_xml_invalid
from ayvu.formats.epub.reader import read_package
from ayvu.knowledge.glossary import GlossaryUsage, apply_glossary_with_usage
from ayvu.providers.libretranslate.client import TranslatorError
from ayvu.translation import (
    ALT_TEXT_KIND, METADATA_KIND, STATUS_CACHE, STATUS_MISSING, STATUS_TRANSLATED, BookRefused, LegacyTextPipelineFacade,
    LinguisticSource, TranslationResources, UnitExecution, to_report,
)
from ayvu.kernel import JobFailure, JobFailureFamily, LanguagePair, TranslationOptions
from tests.support.legacy_cache_file import LegacyCacheFile, LegacyCachePort


class EngineUnitExecutor:
    """One unit through the text pipeline: the cache file first, then the translator, then the glossary."""

    def __init__(self, translator: object, cache: LegacyCacheFile, options: TranslationOptions, *, glossary: object | None = None) -> None:
        self._options = options
        self._stages = LegacyTextPipelineFacade(translator, LegacyCachePort(cache),
                                                lambda translated: apply_glossary_with_usage(translated, glossary))

    def __repr__(self) -> str:
        return "EngineUnitExecutor()"

    def untranslated(self, unit) -> str:
        return html_unit_template(unit)

    def execute(self, unit) -> UnitExecution:
        options = self._options
        template = html_unit_template(unit)
        outcome = self._stages.run(template, LanguagePair(options.source, options.target), cache_only=options.cache_only,
                                   chunk_limit=options.chunk_limit)
        missing = outcome.source is LinguisticSource.CACHE_ONLY_MISSING
        status = STATUS_MISSING if missing else STATUS_CACHE if outcome.source is LinguisticSource.EXACT_CACHE else STATUS_TRANSLATED
        usage = GlossaryUsage() if outcome.glossary_usage is None else outcome.glossary_usage
        text, kind = outcome.text, unit.content.kind.name
        if kind == METADATA_KIND:
            if without_xml_invalid(text) != text:
                raise ValueError("the title translation holds characters XML cannot store")
            return UnitExecution(template if missing else text, status, template, text, text if missing else None, None, usage)
        markup = [unit.source_text[part.start:part.end] for part in unit.content.protected_parts]
        original = review_text_from_template(template, markup)
        rebuilt = template if missing else without_xml_invalid(text) if kind == ALT_TEXT_KIND else normalize_translated_text(text)
        return UnitExecution(rebuilt, status, original, review_text_from_template(text, markup),
                             original if missing else None, None, usage)


class EnginePool:
    """Each worker gets its own translator and its own connection to the cache file."""

    def __init__(self, translator_factory, cache_factory, options, *, glossary=None) -> None:
        self._translator_factory, self._cache_factory = translator_factory, cache_factory
        self._options, self._glossary = options, glossary

    def __repr__(self) -> str:
        return "EnginePool()"

    @contextmanager
    def worker(self) -> Iterator[EngineUnitExecutor]:
        translator = self._translator_factory()
        with self._cache_factory() as cache:
            yield EngineUnitExecutor(translator, cache, self._options, glossary=self._glossary)


class EngineTestRuntime:
    """``prepare`` runs once, inside the job; what it raises is kept for the caller, as a runtime does."""

    def __init__(self, source_path: Path, options: TranslationOptions, prepare: Callable[[], LegacyResources],
                 cache_factory: Callable[[], LegacyCacheFile]) -> None:
        self._source_path, self._options = source_path, options
        self._prepare, self._cache_factory = prepare, cache_factory
        self._prepared: LegacyResources | None = None
        self.preflight_error: Exception | None = None

    def __repr__(self) -> str:
        return "EngineTestRuntime()"

    def preflight(self) -> JobFailure | None:
        try:
            self._prepared = self._prepare()
            if self._options.workers < 1:
                raise ValueError("worker count must be 1 or greater")
            if self._options.workers > 1 and self._prepared.translator_factory is None:
                raise ValueError("translator_factory is required when workers is greater than 1")
        except BookRefused as error:
            self.preflight_error = error
            return JobFailure(JobFailureFamily.INPUT, error.code)
        except Exception as error:
            self.preflight_error = error
            return JobFailure(JobFailureFamily.RESOURCE, "preflight_failed")
        return None

    def facts(self):
        return read_package(self._source_path).facts

    @contextmanager
    def resources(self, context=None) -> Iterator[TranslationResources]:
        prepared, options = self._prepared, self._options
        pool = None
        if options.workers > 1:
            pool = EnginePool(prepared.translator_factory, self._cache_factory, options, glossary=prepared.glossary)
        with self._cache_factory() as cache:
            yield TranslationResources(EngineUnitExecutor(prepared.translator, cache, options, glossary=prepared.glossary), pool)

    def classify(self, error: Exception) -> JobFailure:
        if isinstance(error, TranslatorError):
            return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_failed", retryable=True)
        return JobFailure(JobFailureFamily.INTERNAL, "text_failed")

    def report(self, result, destination: Path):
        glossary = None if self._prepared is None else self._prepared.glossary
        return to_report(result.engine, self._source_path, destination, read_package(self._source_path), self._options, glossary)
