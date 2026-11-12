from __future__ import annotations
from tests.support.workflow_characterization_support import FailingSession, _direct_languages, _isolated_from_the_user
from tests.support.workflow_characterization_support import _members, _report_value, _run_translate
from tests.support.workflow_characterization_support import _write_epub


def test_translate_cli_reports_what_it_did(tmp_path, monkeypatch):
    result, session, output, source = _run_translate(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert _report_value(result.output, "Chapters processed") == "2"
    assert _report_value(result.output, "Texts translated") == str(len(session.posts))
    assert _report_value(result.output, "Errors") == "0"
    assert _report_value(result.output, "Validation warnings") == "0"
    assert list(_members(output)) == list(_members(source))


def test_translate_cli_leaves_title_navigation_and_alt_alone_by_default(tmp_path, monkeypatch):
    result, session, output, source = _run_translate(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    written, original = _members(output), _members(source)
    assert written["OEBPS/content.opf"] == original["OEBPS/content.opf"]
    assert written["OEBPS/nav.xhtml"] == original["OEBPS/nav.xhtml"]
    assert 'alt="pixel"' in written["OEBPS/text/chapter1.xhtml"].decode()
    assert not [payload for payload in session.posts if "Characterization Book" in payload["q"] or payload["q"] == "pixel"]


def test_translate_cli_translates_title_navigation_and_alt_when_asked(tmp_path, monkeypatch):
    result, session, output, _ = _run_translate(tmp_path, monkeypatch, "--translate-metadata", "--translate-alt-text")

    assert result.exit_code == 0, result.output
    written = _members(output)
    assert b"<dc:title>PT:Characterization Book</dc:title>" in written["OEBPS/content.opf"]
    assert b"PT:" in written["OEBPS/nav.xhtml"]
    assert 'alt="PT:pixel"' in written["OEBPS/text/chapter1.xhtml"].decode()
    assert _report_value(result.output, "Alt texts translated") == "1"
    assert session.posts[0]["q"] == "Characterization Book"


def test_translate_cli_translates_only_the_selected_chapter(tmp_path, monkeypatch):
    result, session, output, source = _run_translate(tmp_path, monkeypatch, "--chapters", "2")

    assert result.exit_code == 0, result.output
    written, original = _members(output), _members(source)
    assert written["OEBPS/text/chapter1.xhtml"] == original["OEBPS/text/chapter1.xhtml"]
    assert b"PT:Goodbye reader." in written["OEBPS/text/chapter2.xhtml"]
    assert _report_value(result.output, "Chapters processed") == "1"
    assert not [payload for payload in session.posts if "Hello" in payload["q"]]


def test_translate_cli_refuses_a_selection_that_matches_nothing(tmp_path, monkeypatch):
    result, session, output, _ = _run_translate(tmp_path, monkeypatch, "--chapters", "99")

    assert result.exit_code == 1
    assert "no chapters matched selection: 99" in " ".join(result.output.split())
    assert not output.exists() and not session.posts


def test_translate_cli_dry_run_writes_nothing_and_calls_nothing(tmp_path, monkeypatch):
    result, session, output, _ = _run_translate(tmp_path, monkeypatch, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not output.exists() and not session.posts
    assert not list((tmp_path / "processing").glob("projects/*/project.json"))


def test_translate_cli_cache_only_uses_the_cache_and_lists_what_is_missing(tmp_path, monkeypatch):
    source = _write_epub(tmp_path / "book.epub")
    first, session, _, _ = _run_translate(tmp_path, monkeypatch, "--chapters", "1", source_path=source)
    assert first.exit_code == 0, first.output
    calls = len(session.posts)

    missing = tmp_path / "missing.txt"
    result, session, output, _ = _run_translate(
        tmp_path, monkeypatch, "--cache-only", "--overwrite", "--missing-output", str(missing), source_path=source, session=session)

    assert result.exit_code == 0, result.output
    assert len(session.posts) == calls
    written = _members(output)
    assert b"PT:Hello title" in written["OEBPS/text/chapter1.xhtml"]
    assert b"PT:" not in written["OEBPS/text/chapter2.xhtml"]
    assert _report_value(result.output, "Texts missing") == "3"
    assert "Goodbye reader." in missing.read_text(encoding="utf-8")


def test_translate_cli_require_full_cache_writes_nothing_when_something_is_missing(tmp_path, monkeypatch):
    result, session, output, _ = _run_translate(tmp_path, monkeypatch, "--cache-only", "--require-full-cache")

    assert result.exit_code == 1
    assert not output.exists() and not session.posts


def test_translate_cli_keeps_going_after_a_failed_text_and_reports_it(tmp_path, monkeypatch):
    session = FailingSession(_direct_languages(), "Goodbye reader.")
    result, _, output, _ = _run_translate(tmp_path, monkeypatch, session=session)

    assert _report_value(result.output, "Errors") == "1"
    written = _members(output)
    assert b"<p>Goodbye reader.</p>" in written["OEBPS/text/chapter2.xhtml"]
    assert b"PT:Chapter two" in written["OEBPS/text/chapter2.xhtml"]
    assert b"PT:Hello title" in written["OEBPS/text/chapter1.xhtml"]


def test_translate_cli_fail_fast_stops_without_an_output(tmp_path, monkeypatch):
    session = FailingSession(_direct_languages(), "Goodbye reader.")
    result, _, output, _ = _run_translate(tmp_path, monkeypatch, "--fail-fast", session=session)

    assert result.exit_code != 0
    assert not output.exists()


def test_translate_cli_refuses_to_replace_an_output_unless_told(tmp_path, monkeypatch):
    output = tmp_path / "out.epub"
    output.write_bytes(b"precious")
    result, session, _, _ = _run_translate(tmp_path, monkeypatch)

    assert result.exit_code == 1
    assert output.read_bytes() == b"precious" and not session.posts

    replaced, _, _, _ = _run_translate(tmp_path, monkeypatch, "--overwrite")
    assert replaced.exit_code == 0, replaced.output
    assert output.read_bytes()[:2] == b"PK"


def test_translate_cli_keeps_the_output_and_exits_one_on_validation_warnings(tmp_path, monkeypatch):
    source = _write_epub(tmp_path / "book.epub", chapter_two='<p><a href="gone.xhtml">Broken link</a></p>')
    result, _, output, _ = _run_translate(tmp_path, monkeypatch, source_path=source)

    assert result.exit_code == 1
    assert output.exists()
    assert _report_value(result.output, "Validation warnings") == "1"
    assert "gone.xhtml" in result.output


def test_translate_cli_with_workers_matches_a_sequential_run(tmp_path, monkeypatch):
    sequential, _, output, source = _run_translate(tmp_path, monkeypatch, "--workers", "1")
    assert sequential.exit_code == 0, sequential.output
    expected = _members(output)
    output.unlink()
    (tmp_path / "cache.sqlite").unlink()

    parallel, _, output, _ = _run_translate(tmp_path, monkeypatch, "--workers", "2", source_path=source)

    assert parallel.exit_code == 0, parallel.output
    assert _members(output) == expected


def test_translate_cli_translation_memory_reuses_a_similar_text(tmp_path, monkeypatch):
    first_book = _write_epub(tmp_path / "first.epub", chapter_one="<p>The quick brown fox jumps over the lazy dog</p>")
    first, session, output, _ = _run_translate(tmp_path, monkeypatch, source_path=first_book)
    assert first.exit_code == 0, first.output
    output.unlink()
    second_book = _write_epub(tmp_path / "second.epub", chapter_one="<p>The quick brown fox jumps over the lazy dog!</p>")
    session.posts.clear()

    result, session, output, _ = _run_translate(
        tmp_path, monkeypatch, "--translation-memory", "--tm-apply-threshold", "0.9", "--tm-suggest-threshold", "0.8",
        source_path=second_book, session=session)

    assert result.exit_code == 0, result.output
    assert _report_value(result.output, "Texts from memory") == "1"
    assert b"PT:The quick brown fox jumps over the lazy dog" in _members(output)["OEBPS/text/chapter1.xhtml"]


def test_translate_cli_never_changes_the_original_even_when_it_fails(tmp_path, monkeypatch):
    source = _write_epub(tmp_path / "book.epub")
    before = source.read_bytes()
    session = FailingSession(_direct_languages(), "Hello title")
    _run_translate(tmp_path, monkeypatch, "--fail-fast", session=session, source_path=source)

    assert source.read_bytes() == before


def test_translate_cli_reports_an_unreadable_book_without_a_traceback(tmp_path, monkeypatch):
    source = tmp_path / "broken.epub"
    source.write_bytes(b"not an epub")
    result, session, output, _ = _run_translate(tmp_path, monkeypatch, source_path=source)

    assert result.exit_code == 1
    assert "Traceback" not in result.output and not session.posts and not output.exists()
