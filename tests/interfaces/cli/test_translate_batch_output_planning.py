from tests.support.cli_rich_runtime_support import config
"""Integration checks for complete batch planning and dry-run publications."""

import json
import os
import pytest
from typer.testing import CliRunner
from ayvu.interfaces.cli import app as cli, run as cli_run
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _write_epub
from tests.interfaces.cli.translate_batch_integration_support import _run, isolated_cli


def test_final_conflict_rejects_before_first_item(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    output = tmp_path / "output"
    output.mkdir()
    conflict = output / "second-pt.epub"
    conflict.write_bytes(b"existing")
    calls = []
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, output)

    assert result.exit_code == 1
    assert "existing" in result.output.lower()
    assert calls == []
    assert conflict.read_bytes() == b"existing"
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert not (output / "first-pt.epub").exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_same_stem_destinations_rejected_before_first_item(tmp_path, monkeypatch, overwrite):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    first = _write_epub(left / "book.epub")
    second = _write_epub(right / "book.epub", language="fr")
    calls = []
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: calls.append(kw))
    result = _run(first, second, tmp_path / "out", *( ["--overwrite"] if overwrite else [] ))

    assert result.exit_code == 1
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_dry_run_existing_output_saves_each_report_without_replacing_epub(tmp_path, monkeypatch):
    config(tmp_path, books_dir=str(tmp_path / "home" / "Documentos" / "Livros"))
    first = _write_epub(tmp_path / "first.epub", language="en")
    second = _write_epub(tmp_path / "second.epub", language="fr")
    originals = [first.read_bytes(), second.read_bytes()]
    output = tmp_path / "out"
    output.mkdir()
    existing = output / "second-pt.epub"
    existing.write_bytes(b"existing")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = _run(first, second, output, "--dry-run")

    assert result.exit_code == 0, result.output
    assert existing.read_bytes() == b"existing"
    assert not (output / "first-pt.epub").exists()
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert (reports / "first-pt-report.md").is_file()
    assert (reports / "second-pt-report.md").is_file()
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert not session.posts


def test_output_cannot_replace_another_source_even_with_overwrite(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "book.epub")
    second = _write_epub(tmp_path / "book-pt.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    calls = []
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, tmp_path, "--overwrite")

    assert result.exit_code == 1
    assert calls == []
    assert [first.read_bytes(), second.read_bytes()] == originals


@pytest.mark.parametrize("alias", ["hardlink", "symlink"])
def test_existing_output_alias_to_any_source_rejected(tmp_path, monkeypatch, alias):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    destination = output / "first-pt.epub"
    if alias == "hardlink":
        destination.hardlink_to(second)
    else:
        destination.symlink_to(second)
    before = [first.read_bytes(), second.read_bytes()]
    calls = []
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, output, "--overwrite")

    assert result.exit_code == 1
    assert calls == []
    assert [first.read_bytes(), second.read_bytes()] == before
    assert destination.read_bytes() == before[1]


def test_common_mode_cancel_conflict_on_final_item_starts_nothing(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    conflict = output / "second-pt.epub"
    conflict.write_bytes(b"existing")
    calls = []
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: calls.append(kw))

    result = CliRunner().invoke(cli.app, [
        "--mode", "common", "translate", str(first), str(second),
        "--output-dir", str(output),
    ], input="0\n")

    assert result.exit_code == 1
    assert "Choose an option" in result.output
    assert calls == []
    assert conflict.read_bytes() == b"existing"


def test_cache_only_dry_run_keeps_reports_and_automatic_missing_files(tmp_path, monkeypatch):
    config(tmp_path, books_dir=str(tmp_path / "home" / "Documentos" / "Livros"))
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = _run(first, second, output, "--dry-run", "--cache-only", "--cache", str(tmp_path / "cache.sqlite"))

    assert result.exit_code == 0, result.output
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert sorted(p.name for p in reports.glob("*.md")) == ["first-pt-report.md", "second-pt-report.md"]
    assert sorted(p.name for p in reports.glob("*.txt")) == ["first-pt-missing.txt", "second-pt-missing.txt"]
    assert not list(output.glob("*.epub"))
    assert not session.posts


def test_automatic_reports_and_missing_files_follow_the_umask_like_any_other_output(tmp_path, monkeypatch):
    config(tmp_path, books_dir=str(tmp_path / "home" / "Documentos" / "Livros"))
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    _install_cli_environment(monkeypatch, tmp_path, FakeLibreTranslateSession(_direct_languages()))
    previous = os.umask(0o027)
    try:
        result = _run(first, second, tmp_path / "out", "--dry-run", "--cache-only", "--cache", str(tmp_path / "cache.sqlite"))
    finally:
        os.umask(previous)

    assert result.exit_code == 0, result.output
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert {item.stat().st_mode & 0o777 for item in reports.iterdir()} == {0o640}


def test_real_project_batch_resolves_each_source_language(tmp_path, monkeypatch):
    config(tmp_path, books_dir=str(tmp_path / "home" / "Documentos" / "Livros"))
    first = _write_epub(tmp_path / "first.epub", language="en")
    second = _write_epub(tmp_path / "second.epub", language="fr")
    originals = [first.read_bytes(), second.read_bytes()]
    languages = _direct_languages() + [{"code": "fr", "name": "French", "targets": ["pt"]}]
    session = FakeLibreTranslateSession(languages)
    processing = _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 0, result.output
    manifests = [json.loads(path.read_text()) for path in processing.glob("projects/*/project.json")]
    assert {p["source"] for p in manifests} == {"en", "fr"}
    assert len({p["project_id"] for p in manifests}) == 2
    assert {item["source"] for item in session.posts} == {"en", "fr"}
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert sorted(p.name for p in output.glob("*.epub")) == ["first-pt.epub", "second-pt.epub"]


def test_common_rename_and_overwrite_are_decided_before_translation(tmp_path, monkeypatch):
    config(tmp_path, books_dir=str(tmp_path / "home" / "Documentos" / "Livros"))
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    old_first = output / "first-pt.epub"
    old_second = output / "second-pt.epub"
    old_first.write_bytes(b"first old")
    old_second.write_bytes(b"second old")
    renamed = output / "renamed.epub"
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = CliRunner().invoke(cli.app, [
        "--mode", "common", "translate", str(first), str(second), "--output-dir", str(output),
        "--source", "en", "--cache", str(tmp_path / "cache.sqlite"), "--url", "http://localhost:5000", "--retries", "0",
    ], input=f"2\n{renamed}\n1\n")

    assert result.output.count("Choose an option") == 2
    assert result.output.rfind("Choose an option") < result.output.index("Batch item 1/2")
    assert result.exit_code == 0, result.output
    assert old_first.read_bytes() == b"first old"
    assert old_second.read_bytes().startswith(b"PK")
    assert renamed.read_bytes().startswith(b"PK")
    assert session.posts


def test_common_refusal_has_no_empty_batch_summary(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    (output / "second-pt.epub").write_bytes(b"old")
    monkeypatch.setattr(cli_run, "_run_translation", lambda **kw: pytest.fail("item started"))

    result = CliRunner().invoke(cli.app, ["--mode", "common", "translate", str(first), str(second),
                                          "--output-dir", str(output)], input="0\n")

    assert result.exit_code == 1
    assert "Batch translation summary" not in result.output
