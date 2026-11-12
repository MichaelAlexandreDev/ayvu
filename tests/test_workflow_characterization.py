from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
import requests
from typer.testing import CliRunner

from ayvu.interfaces.cli.app import app
from ayvu.v1.resume import COMPLETED_STATUS, RUNNING_STATUS, ResumeStateStore
from ayvu.review import decode_review_package, encode_review_package

from tests.support.workflow_characterization_support import FailingSession, FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _isolated_from_the_user
from tests.support.workflow_characterization_support import _members, _read_member, _report_value, _run_translate, _translate_args
from tests.support.workflow_characterization_support import _write_epub, _zip_metadata, runner


def test_translate_cli_preserves_source_archive_and_visible_text(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "book.epub")
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    session = FakeLibreTranslateSession(_direct_languages())
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, session)

    output_dir = tmp_path / "translated"
    result = runner.invoke(
        app,
        _translate_args(source_path, tmp_path / "cache.sqlite", output_dir=output_dir),
    )

    output_path = output_dir / "book-pt.epub"
    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "Validação OK" in result.output
    assert source_path.read_bytes() == source_bytes
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    manifests = list(processing_dir.glob('projects/*/project.json'))
    assert len(manifests) == 1
    project = json.loads(manifests[0].read_text())
    assert project['source_sha256'] == source_hash
    assert project['source_path'] == str(source_path)
    assert project['target'] == 'pt'
    assert project['project_id'] == manifests[0].parent.name

    chapter = _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert "PT:Hello title" in chapter
    assert "PT:Hello <a href=\"chapter2.xhtml#two\">reader</a>." in chapter
    assert "PT:Game Loop uses Observer." in chapter
    for hidden_text in ("script keep", "code keep", "pre keep", "kbd keep", "samp keep", "svg keep", "math keep"):
        assert hidden_text in chapter

    submitted_text = "\n".join(payload["q"] for payload in session.posts)
    assert "Hello " in submitted_text
    assert "reader" in submitted_text
    for hidden_text in ("script keep", "code keep", "pre keep", "kbd keep", "samp keep", "svg keep", "math keep"):
        assert hidden_text not in submitted_text

    unchanged_members = (
        "OEBPS/styles/book.css",
        "OEBPS/images/pixel.png",
        "OEBPS/assets/keep.bin",
    )
    with ZipFile(source_path) as source, ZipFile(output_path) as output:
        assert source.namelist() == output.namelist()
        assert output.infolist()[0].filename == "mimetype"
        assert output.getinfo("mimetype").compress_type == ZIP_STORED
        assert output.read("mimetype") == b"application/epub+zip"
        for member in unchanged_members:
            assert output.read(member) == source.read(member)
            assert _zip_metadata(output.getinfo(member)) == _zip_metadata(source.getinfo(member))


def test_translate_cli_reuses_exact_cache_and_applies_glossary_after_cache(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "book.epub")
    cache_path = tmp_path / "cache.sqlite"
    session = FakeLibreTranslateSession(_direct_languages())
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, session)

    first_output = tmp_path / "first.epub"
    first = runner.invoke(app, _translate_args(source_path, cache_path, output_path=first_output))
    assert first.exit_code == 0, first.output
    post_count_after_first_run = len(session.posts)

    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(json.dumps({"Game Loop": "loop de jogo"}), encoding="utf-8")
    second_output = tmp_path / "second.epub"
    second = runner.invoke(
        app,
        _translate_args(
            source_path,
            cache_path,
            output_path=second_output,
            glossary_path=glossary_path,
        ),
    )

    assert second.exit_code == 0, second.output
    assert len(session.posts) == post_count_after_first_run
    assert "Texts from cache" in second.output
    assert "PT:Game Loop uses Observer." in _read_member(first_output, "OEBPS/text/chapter1.xhtml")
    assert "PT:loop de jogo uses Observer." in _read_member(second_output, "OEBPS/text/chapter1.xhtml")
    # New glossary intent gets a distinct project, preserving the first one.
    manifests = list(processing_dir.glob('projects/*/project.json'))
    assert len(manifests) == 2
    assert len({json.loads(path.read_text())['project_id'] for path in manifests}) == 2
    third_output = tmp_path / 'third.epub'
    third = runner.invoke(app, _translate_args(source_path, cache_path, output_path=third_output,
                                               glossary_path=glossary_path))
    assert third.exit_code == 0, third.output
    assert len(list(processing_dir.glob('projects/*/project.json'))) == 2
    assert len(session.posts) == post_count_after_first_run


def test_translate_cli_interrupts_then_resume_reuses_checkpoint_and_cache(tmp_path, monkeypatch):
    source_path = _write_epub(
        tmp_path / "interrupted.epub",
        chapter_one="<p>First chapter text.</p>",
        chapter_two='<p id="two">Interrupt when translating this sentence.</p>',
    )
    cache_path = tmp_path / "cache.sqlite"
    output_path = tmp_path / "interrupted-pt.epub"
    interrupted_session = FakeLibreTranslateSession(
        _direct_languages(),
        interrupt_on_text="Interrupt when translating",
    )
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, interrupted_session)

    interrupted = runner.invoke(
        app,
        _translate_args(source_path, cache_path, output_path=output_path),
    )

    assert interrupted.exit_code == 1
    assert "Translation interrupted by user." in interrupted.output
    assert not output_path.exists()
    scan = ResumeStateStore(processing_dir).scan()
    assert len(scan.running) == 1
    state = scan.running[0]
    assert state.status == RUNNING_STATUS
    assert state.completed_chapters
    assert state.current_chapter is not None

    resumed_session = FakeLibreTranslateSession(_direct_languages())
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: resumed_session)
    resumed = runner.invoke(
        app,
        ["--mode", "developer", "resume", str(source_path), "--target", "pt"],
    )

    assert resumed.exit_code == 0, resumed.output
    assert output_path.exists()
    resumed_text = "\n".join(payload["q"] for payload in resumed_session.posts)
    assert "First chapter text." not in resumed_text
    assert "Interrupt when translating this sentence." in resumed_text
    completed = ResumeStateStore(processing_dir).scan()
    assert completed.running == ()
    state_path = processing_dir / "interrupted-pt.ayvu-state.json"
    assert ResumeStateStore(processing_dir).load(state_path).status == COMPLETED_STATUS
    assert "PT:First chapter text." in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert "PT:Interrupt when translating this sentence." in _read_member(
        output_path,
        "OEBPS/text/chapter2.xhtml",
    )


def test_translate_cli_resolves_intermediate_route_end_to_end(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "route.epub", language="fr")
    session = FakeLibreTranslateSession(
        [
            {"code": "fr", "name": "French", "targets": ["en"]},
            {"code": "en", "name": "English", "targets": ["pt"]},
            {"code": "pt", "name": "Portuguese", "targets": []},
        ]
    )
    _install_cli_environment(monkeypatch, tmp_path, session)
    output_path = tmp_path / "route-pt.epub"

    result = runner.invoke(
        app,
        _translate_args(
            source_path,
            tmp_path / "cache.sqlite",
            output_path=output_path,
            source="fr",
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Route: fr -> en -> pt" in result.output
    assert "PT:EN:Hello title" in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    pairs = {(payload["source"], payload["target"]) for payload in session.posts}
    assert ("fr", "en") in pairs
    assert ("en", "pt") in pairs


def test_cli_review_export_is_v2_and_apply_reconstructs_only_after_validation(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "reviewable.epub")
    source_bytes = source_path.read_bytes()
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    translated_path = tmp_path / "reviewable-pt.epub"
    review_path = tmp_path / "review.csv"

    translated = runner.invoke(
        app,
        _translate_args(
            source_path,
            tmp_path / "cache.sqlite",
            output_path=translated_path,
            review_path=review_path,
        ),
    )
    assert translated.exit_code == 0, translated.output
    assert review_path.exists()
    package_bytes = review_path.read_bytes()
    assert package_bytes.startswith(b"AYVU-REVIEW-PACKAGE,2,")
    package = decode_review_package(package_bytes)
    edited = replace(package, rows=tuple(
        replace(row, reviewed_text=row.machine_translation.replace("PT:", "Reviewed:"), review_status="reviewed")
        for row in package.rows))
    review_path.write_bytes(encode_review_package(edited))

    output_path = tmp_path / "reviewable-pt-reviewed.epub"
    preview = runner.invoke(app, ["--mode", "developer", "apply-review", str(source_path), str(review_path),
                                  "--validate-only"])
    reviewed = runner.invoke(app, ["--mode", "developer", "apply-review", str(source_path), str(review_path),
                                   "--output", str(output_path)])

    assert preview.exit_code == 0, preview.output
    assert "Review package validation" in " ".join(preview.output.split())
    assert reviewed.exit_code == 0, reviewed.output
    assert "Review application report" in reviewed.output
    assert output_path.exists()
    assert 'Reviewed:Hello <a href="chapter2.xhtml#two">reader</a>.' in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert source_path.read_bytes() == source_bytes


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


def test_batch_translate_cli_translates_each_book_into_the_directory(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    target = tmp_path / "batch"
    args = _translate_args(first, tmp_path / "cache.sqlite", output_dir=target)
    position = args.index(str(first))
    result = runner.invoke(app, [*args[:position + 1], str(second), *args[position + 1:]])

    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in target.glob("*.epub")) == ["first-pt.epub", "second-pt.epub"]


def test_preview_cli_translates_only_the_first_documents_and_keeps_project_state_empty(tmp_path, monkeypatch):
    source = _write_epub(tmp_path / "book.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    processing = _install_cli_environment(monkeypatch, tmp_path, session)
    result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])

    assert result.exit_code == 0, result.output
    assert session.posts
    preview = tmp_path / "home" / "Documentos" / "Livros" / "Preview" / "book-preview.epub"
    assert b"PT:Hello title" in _members(preview)["OEBPS/text/chapter1.xhtml"]
    assert not list(processing.glob("projects/*/project.json"))


def test_these_tests_cannot_reach_the_users_folders(tmp_path):
    assert Path.home() == tmp_path / "home" and Path.cwd() == tmp_path
