from __future__ import annotations
from pathlib import Path
from ayvu.interfaces.cli.app import app
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _isolated_from_the_user
from tests.support.workflow_characterization_support import _members, _translate_args
from tests.support.workflow_characterization_support import _write_epub, runner


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
