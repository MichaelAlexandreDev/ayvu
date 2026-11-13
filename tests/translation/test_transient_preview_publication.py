"""Selection parity, raw reuse and transactional local preview publication."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from ayvu.composition.cli import _translate_without_project
from ayvu.composition.builders.translate_project import translate_without_project
from ayvu.composition.builders.transient_publication import LocalTransientPublication
from ayvu.formats.epub.io import preview_documents
from ayvu.formats.epub.validation import ValidationResult, validate_output_epub
from ayvu.formats.local import LocalFormatSource
from ayvu.providers.libretranslate.client import RequestRateLimiter
from ayvu.kernel import ChapterSelection, LanguagePair, TranslationOptions
from ayvu.translation import PreflightError, TranslationDryRunCancelled, TranslationExecutionError
from ayvu.v1.cache import TranslationCache
from tests.support.rich_preview_support import (
    HOOKS, KEYS, ORDER, PreviewTransport, book_request, members, preview_book, preview_guard, seed_preview,
)


@pytest.mark.parametrize("limit", [12, 1, 0, -3, None])
def test_v1_document_enumeration_and_limits_match_rich_preview(tmp_path, monkeypatch, limit):
    source = preview_book(tmp_path / "book.epub")
    original = members(source)
    options = TranslationOptions(LanguagePair("en", "pt"), max_documents=limit)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, options=options)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        outcome = _translate_without_project(book, HOOKS)
    count = len(KEYS) if limit is None else max(0, limit)
    expected = list(KEYS[:count])
    assert [item.archive_path for item in preview_documents(source, max_documents=limit)] == expected
    assert session.posts == [f"Document {i:02}." for i in ORDER[:count]]
    legacy_order = []
    legacy_prepared = book.prepare()
    try:
        with TranslationCache(tmp_path / "v1.sqlite") as cache:
            translate_without_project(source, tmp_path / "v1.epub", translator=legacy_prepared.online.translator,
                cache=cache, options=options, glossary=legacy_prepared.glossary,
                on_chapter_start=lambda i, total, name: legacy_order.append("OEBPS/" + name))
    finally:
        legacy_prepared.online.translator.close()
    assert legacy_order == expected
    after = members(book.output_plan.path)
    assert {key for key in original if original[key] != after[key]} == set(expected)
    assert outcome.report.chapters_processed == len(expected) and outcome.report.output_written
    assert validate_output_epub(book.output_plan.path).ok


@pytest.mark.parametrize("selection,limit,expected", [("2", 1, None), ("1", 1, KEYS[:1]), ("13", 12, None)])
def test_chapter_selection_is_applied_after_the_limit(tmp_path, monkeypatch, selection, limit, expected):
    source = preview_book(tmp_path / "book.epub")
    options = TranslationOptions(LanguagePair("en", "pt"), max_documents=limit,
                                 chapter_selection=ChapterSelection.parse(selection))
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, options=options)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path, published=expected is not None):
        if expected is None:
            from ayvu.kernel import ChapterSelectionError
            with pytest.raises(ChapterSelectionError, match="no chapters matched"):
                _translate_without_project(book, HOOKS)
        else:
            assert _translate_without_project(book, HOOKS).report.chapters_processed == 1
    assert len(session.posts) == (0 if expected is None else 1) and session.closed == 1


def test_metadata_does_not_consume_the_document_limit(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub")
    options = TranslationOptions(LanguagePair("en", "pt"), max_documents=1, translate_metadata=True)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, options=options)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        result = _translate_without_project(book, HOOKS)
    assert session.posts == ["My Title", "Document 07."] and result.report.chapters_processed == 1


def test_persistent_cache_reapplies_glossary_without_any_write(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub", repeated=True)
    glossary = tmp_path / "glossary.json"
    glossary.write_text('{"reader": "CURRENT-READER"}')
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, glossary=glossary)
    seed_preview(book, first_only=True)
    session.posts.clear()
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path) as sql:
        report = _translate_without_project(book, HOOKS).report
    assert report.texts_from_cache == 12 and not session.posts
    assert any("SELECT" in statement and "cache_entries" in statement for statement in sql)
    assert all(b"CURRENT-READER" in members(book.output_plan.path)[key] for key in KEYS[:12])


def test_overlay_stores_provider_raw_before_the_current_glossary(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub", repeated=True)
    glossary = tmp_path / "glossary.json"
    glossary.write_text('{"reader": "reader-current"}')
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, glossary=glossary)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        report = _translate_without_project(book, HOOKS).report
    assert session.posts == ["Repeated reader."] and report.texts_from_cache == 11
    after = members(book.output_plan.path)
    assert all(b"PT:Repeated reader-current." in after[key] and b"current-current" not in after[key] for key in KEYS[:12])


def test_v2_hit_and_overlay_misses_share_a_read_only_invocation(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub", repeated=True, distinct_first=True)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source)
    seed_preview(book, first_only=True)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path) as sql:
        report = _translate_without_project(book, HOOKS).report
    assert session.posts == ["Repeated reader."]
    assert report.texts_from_cache == 11 and report.texts_translated == 1
    assert len([statement for statement in sql if statement.startswith("SELECT length(CAST(source_text")
                and "FROM cache_entries_v2 WHERE" in statement]) == 12


def test_existing_v2_without_a_catalog_grants_no_persistent_reads(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub", repeated=True)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source)
    seed_preview(book)
    (tmp_path / "processing").rename(tmp_path / "other-processing")
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path) as sql:
        report = _translate_without_project(book, HOOKS).report
    assert not sql and session.posts == ["Repeated reader."]
    assert report.texts_translated == 1 and report.texts_from_cache == 11


def test_persistent_miss_uses_overlay_once_and_discards_it_at_end(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub", repeated=True)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source)
    project, _ = seed_preview(book)
    # Keep the matching manifest but remove v2 before the invocation.
    cache_path = book.cache_path.with_name(book.cache_path.name + ".v2.sqlite")
    cache_path.unlink()
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        report = _translate_without_project(book, HOOKS).report
    assert report.texts_translated == 1 and report.texts_from_cache == 11
    assert session.posts == ["Repeated reader."]
    second = replace(book, replace_existing=True)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        _translate_without_project(second, HOOKS)
    assert session.posts == ["Repeated reader.", "Repeated reader."] and not cache_path.exists()


@pytest.mark.parametrize("point", ["engine_before_unit", "provider_before_request", "engine_before_publication", "epub_before_output"])
def test_cancellation_never_publishes_or_creates_output_directories(tmp_path, monkeypatch, point):
    source = preview_book(tmp_path / "book.epub")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    class Cancel:
        def checkpoint(self, safe_point):
            return object() if safe_point == point else None
    book = book_request(tmp_path, source, cancellation=Cancel())
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path, published=False):
        with pytest.raises((TranslationDryRunCancelled, PreflightError)):
            _translate_without_project(book, HOOKS)
    assert not book.output_plan.path.parent.exists() and session.closed == 1


def test_failed_final_validation_keeps_existing_destination_and_removes_staging(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, overwrite=True)
    destination = book.output_plan.path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous preview")
    monkeypatch.setattr("ayvu.composition.builders.transient_publication.validate_output_epub",
                        lambda _: ValidationResult(False))
    with pytest.raises(PreflightError) as failure:
        _translate_without_project(book, HOOKS)
    assert failure.value.detail == "invalid_output"
    assert destination.read_bytes() == b"previous preview"
    assert list(destination.parent.iterdir()) == [destination]


def test_source_alias_is_refused_before_any_output_io(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source, overwrite=True)
    book = replace(book, output_plan=replace(book.output_plan, path=source))
    with preview_guard(monkeypatch, tmp_path, source, published=False):
        with pytest.raises(PreflightError) as failure:
            _translate_without_project(book, HOOKS)
    assert failure.value.detail == "output_is_source"


@pytest.mark.parametrize("bridge", [False, True])
def test_preview_uses_one_rate_limiter_for_discovery_and_every_hop(tmp_path, monkeypatch, bridge):
    source = preview_book(tmp_path / "book.epub")
    class Session(PreviewTransport):
        def get(self, *args, **kwargs):
            return self._response([
                {"code": "es", "name": "Spanish", "targets": ["en"]},
                {"code": "en", "name": "English", "targets": ["pt"]},
                {"code": "pt", "name": "Portuguese", "targets": []}])
    session = Session()
    monkeypatch.setattr("requests.Session", lambda: session)
    observed = []
    monkeypatch.setattr(RequestRateLimiter, "wait", lambda self: observed.append(self))
    options = TranslationOptions(LanguagePair("es" if bridge else "en", "pt"), max_documents=1)
    book = book_request(tmp_path, source, options=options, requests_per_second=2.5)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path):
        _translate_without_project(book, HOOKS)
    assert len(observed) == (3 if bridge else 2)
    assert len({id(item) for item in observed}) == 1 and observed[0].requests_per_second == 2.5
    assert len(session.posts) == (2 if bridge else 1)


@pytest.mark.parametrize("state,code", [("payload", "cache_entry_corrupt"), ("journal", "cache_recovery_required"),
                                       ("readonly", None)])
def test_preview_preserves_read_only_cache_and_refuses_unsafe_evidence(tmp_path, monkeypatch, state, code):
    source = preview_book(tmp_path / "book.epub", repeated=True)
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source)
    seed_preview(book)
    path = book.cache_path.with_name(book.cache_path.name + ".v2.sqlite")
    if state == "payload":
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE cache_entries_v2 SET value='tampered'")
    elif state == "journal":
        path.with_name(path.name + "-journal").write_bytes(b"abandoned")
    else:
        path.chmod(0o400)
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path, published=code is None):
        if code:
            with pytest.raises(PreflightError) as failure:
                _translate_without_project(book, HOOKS)
            assert failure.value.detail == code
        else:
            assert _translate_without_project(book, HOOKS).report.texts_from_cache == 12
    assert not session.posts


def test_unreadable_matching_manifest_refuses_before_any_provider_text(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    book = book_request(tmp_path, source)
    project, _ = seed_preview(book)
    (tmp_path / "processing" / "projects" / project.project_id / "project.json").write_text("invalid")
    with preview_guard(monkeypatch, tmp_path, book.output_plan.path, published=False) as sql:
        with pytest.raises(PreflightError) as failure:
            _translate_without_project(book, HOOKS)
    assert "rich preview" in failure.value.summary and not session.posts and not sql
    assert session.closed == 2
