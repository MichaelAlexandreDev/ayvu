from __future__ import annotations


import pytest

from ayvu.v1.cache import TranslationCache
from ayvu.formats.local import LocalFormatSource
from ayvu.v1.translation import translate_book
from tests.support.epub_fakes import epub_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv
from ayvu.kernel import TranslationMemoryOptions, TranslationOptions, JobFailureFamily

from tests.v1.translation_engine_support import PAIR, Prefix
from tests.v1.translation_engine_support import book, engine_only, formats


def test_workers_each_get_their_own_executor_and_results_stay_in_book_order(tmp_path, formats):
    source = book(tmp_path)
    created: list[Prefix] = []

    def new_translator() -> Prefix:
        created.append(Prefix())
        return created[-1]

    env = FormatEnv(tmp_path / "env")
    starts: list[int] = []
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2), formats=formats,
            source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT,
            on_chapter_start=lambda index, *_: starts.append(index), translator_factory=new_translator,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert result.failure is None and starts == [1, 2] and len(created) == 2


def test_a_worker_that_cannot_start_leaves_its_chapter_alone_and_is_reported(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    created = []

    def new_translator() -> Prefix:
        created.append(1)
        if len(created) == 1:
            raise OSError("no session")
        return Prefix()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=1 + 1), formats=formats,
            source=LocalFormatSource(source), publication=publication, project_id=PROJECT, translator_factory=new_translator,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert result.failure is None and len(report.errors) == 1 and "no session" in report.errors[0]


def test_no_chapter_finished_means_no_output(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()

    def broken() -> Prefix:
        raise OSError("no session")

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2), formats=formats,
            source=LocalFormatSource(source), publication=publication, project_id=PROJECT, translator_factory=broken,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INPUT, "nothing_to_materialize")
    assert not report.output_written and not env.is_ready(publication)


def test_memory_and_workers_are_refused_as_in_the_current_flow(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    options = TranslationOptions(language_pair=PAIR, workers=2, translation_memory=TranslationMemoryOptions())
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ValueError, match="translation memory is not supported"):
        translate_book(source, tmp_path / "out.epub", Prefix(), cache, options, formats=formats, source=LocalFormatSource(source),
                       publication=env.publication(), project_id=PROJECT, translator_factory=Prefix,
                       cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))


def test_results_omit_book_text_from_their_repr(tmp_path, formats):
    _, result, _, _ = engine_only(tmp_path, formats, epub_bytes(), translate_metadata=True)
    shown = repr(result) + repr(result.stats)
    assert "Title" not in shown and "Hello" not in shown
