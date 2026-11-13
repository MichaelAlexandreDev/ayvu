"""Offline extraction, precedence, protection and report conversion share rich stages."""
from dataclasses import replace
import json

import pytest

from ayvu.kernel import ChapterSelection, LanguagePair, TranslationOptions
from ayvu.translation import EngineObserver, TranslationDryRunCancelled
from tests.support.transient_translation_support import _book, seed, execute, effect_guard


def options(**changes):
    return TranslationOptions(LanguagePair("en", "pt"), dry_run=True, **changes)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("cache_only", [False, True])
def test_unit_precedence_and_report_conversion(tmp_path, monkeypatch, warm, cache_only):
    source = _book(tmp_path / "book.epub")
    opts = options(cache_only=cache_only)
    project, _ = seed(tmp_path, source, opts, warm=warm)
    statuses, chapters = [], []
    observer = EngineObserver(on_text_processed=statuses.append,
                              on_chapter_done=lambda *args: chapters.append(args[2]))
    with effect_guard(monkeypatch, tmp_path) as sql:
        report = execute(tmp_path, source, opts, project, observer=observer)
    assert len(statuses) == 5 and len(chapters) == report.chapters_processed == 2
    assert set(statuses) == {"cache" if warm else "missing" if cache_only else "dry_run"}
    assert report.texts_from_cache == (5 if warm else 0)
    assert report.texts_missing == (5 if cache_only and not warm else 0)
    assert report.texts_translated == (5 if not warm and not cache_only else 0)
    assert not report.output_written and sql


def test_no_scope_does_not_read_an_existing_database(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    seed(tmp_path, source, options())
    with effect_guard(monkeypatch, tmp_path) as sql:
        report = execute(tmp_path, source, options())
    assert report.texts_from_cache == 0 and report.texts_translated == 5 and not sql


def test_missing_text_is_readable_with_inline_markup_and_unicode(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub", chapter_two="<p> Hello <b>world</b> — café &amp; λ. </p>")
    with effect_guard(monkeypatch, tmp_path):
        report = execute(tmp_path, source, options(cache_only=True))
    assert report.missing_texts[-1] == "Hello world — café & λ."
    assert not any("__AYVU_" in text or "<b>" in text or not text for text in report.missing_texts)


def test_chapter_selection_uses_the_format_inventory(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    with effect_guard(monkeypatch, tmp_path):
        report = execute(tmp_path, source, options(chapter_selection=ChapterSelection.parse("2")))
    assert report.chapters_processed == 1 and report.texts_translated == 2


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_local_documents_keep_order_and_configured_workers(tmp_path, monkeypatch, workers):
    source = _book(tmp_path / "book.epub")
    opts = options(workers=workers)
    seen = []
    with effect_guard(monkeypatch, tmp_path):
        report = execute(tmp_path, source, opts,
            observer=EngineObserver(on_chapter_start=lambda index, total, name: seen.append((index, total, name))))
    assert seen == [(1, 2, "c1.xhtml"), (2, 2, "c2.xhtml")]
    assert opts.workers == workers and report.chapters_processed == 2


def test_glossary_postprocessing_does_not_change_raw_entries(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"reader": "NEW-READER"}))
    project, _ = seed(tmp_path, source, options(), glossary=glossary)
    with effect_guard(monkeypatch, tmp_path):
        report = execute(tmp_path, source, options(), project, glossary=glossary)
    assert report.glossary_usage.total_applied == 2 and report.texts_from_cache == 5


def test_cancellation_stops_before_the_next_unit_without_effects(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    class Cancel:
        def checkpoint(self, point):
            return object() if point == "engine_before_unit" else None
    with effect_guard(monkeypatch, tmp_path), pytest.raises(TranslationDryRunCancelled):
        execute(tmp_path, source, options(), cancellation=Cancel())


def test_empty_language_is_rejected_locally(tmp_path, monkeypatch):
    from ayvu.translation import PreflightError
    source = _book(tmp_path / "book.epub")
    opts = TranslationOptions(LanguagePair("", "pt"), dry_run=True)
    with effect_guard(monkeypatch, tmp_path), pytest.raises(PreflightError):
        execute(tmp_path, source, opts)
