"""Existing missing-text rendering and publication contracts on the rich route."""
from pathlib import Path

import pytest

from ayvu.translation import TranslationReport
from ayvu.interfaces.cli import report as reports
from tests.support.rich_cache_only_support import (
    _book, seed, invoke, config, isolate, text, offline_guard, runner, app, options,
)


def expected(source, texts):
    lines = ["# Ayvu cache-only missing texts", f"# Original EPUB: {source}",
        "# Source language: en", "# Target language: pt", f"# Missing texts: {len(texts)}", ""]
    for index, value in enumerate(texts, 1):
        lines.extend([f"[{index}]", value, ""])
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("full", [False, True])
def test_explicit_missing_file_matches_format_order_and_final_newline(tmp_path, monkeypatch, full):
    config(tmp_path)
    source = _book(tmp_path / "book.epub", chapter_two="<p> Café <b>λ</b> HTTP </p>")
    target = tmp_path / "explicit.txt"
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--missing-output", str(target), *(["--require-full-cache"] if full else []))
    assert result.exit_code == int(full), text(result)
    assert target.read_text() == expected(source, ["T", "Hello title", "Hello reader.", "T", "Café λ HTTP"])


@pytest.mark.parametrize("overwrite", [False, True])
def test_explicit_file_requires_overwrite_and_keeps_original_mode(tmp_path, monkeypatch, overwrite):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    target = tmp_path / "missing.txt"
    target.write_text("previous")
    target.chmod(0o640)
    mode = target.stat().st_mode
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--missing-output", str(target), *(["--overwrite"] if overwrite else []))
    assert result.exit_code == int(not overwrite), text(result)
    assert target.stat().st_mode == mode
    assert ("# Missing texts: 5" in target.read_text()) if overwrite else target.read_text() == "previous"


def test_automatic_name_uses_suffix_and_never_deduplicates_occurrences(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub", chapter_two="<p>Hello reader.</p>")
    directory = tmp_path / "books" / "Relatorios"
    directory.mkdir(parents=True)
    previous = directory / "book-pt-missing.txt"
    previous.write_text("previous")
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source)
    assert result.exit_code == 0, text(result)
    files = sorted(directory.glob("book-pt-missing*.txt"))
    assert len(files) == 2 and previous.read_text() == "previous"
    generated = next(path for path in files if path != previous)
    assert generated.read_text().count("Hello reader.") == 2
    assert "# Missing texts: 5" in generated.read_text()


def test_full_hits_do_not_create_an_explicit_empty_missing_file(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    seed(tmp_path, source, options())
    target = tmp_path / "missing.txt"
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--missing-output", str(target))
    assert result.exit_code == 0, text(result)
    assert not target.exists()


def test_batch_overwrite_uses_accepted_item_auxiliary_publication(tmp_path, monkeypatch):
    from ayvu.translation.batch_output import AcceptedItem
    config(tmp_path)
    first = _book(tmp_path / "first.epub")
    second = _book(tmp_path / "second.epub")
    output = tmp_path / "output"
    output.mkdir()
    for stem in ("first", "second"):
        (output / f"{stem}-pt.epub").write_bytes(b"previous")
    calls = []
    original = AcceptedItem.write_auxiliary
    def write(self, kind, payload):
        calls.append((kind, payload))
        return original(self, kind, payload)
    monkeypatch.setattr(AcceptedItem, "write_auxiliary", write)
    with offline_guard(monkeypatch):
        result = runner.invoke(app, ["--mode", "developer", "translate", str(first), str(second),
            "--source", "en", "--target", "pt", "--cache", str(tmp_path / "cache.sqlite"),
            "--output-dir", str(output), "--cache-only", "--overwrite"])
    assert result.exit_code == 0, text(result)
    assert [kind for kind, _ in calls].count("missing") == 2
    assert len(list((tmp_path / "books" / "Relatorios").glob("*.txt"))) == 2
    assert all(path.read_bytes() != b"previous" for path in output.glob("*.epub"))


def test_missing_output_is_still_rejected_for_multiple_books(tmp_path, monkeypatch):
    config(tmp_path)
    sources = [_book(tmp_path / f"{stem}.epub") for stem in ("first", "second")]
    with offline_guard(monkeypatch):
        result = runner.invoke(app, ["--mode", "developer", "translate", *map(str, sources),
            "--cache-only", "--missing-output", str(tmp_path / "missing.txt")])
    assert result.exit_code == 1 and "--missing-output" in text(result)
    assert not (tmp_path / "missing.txt").exists()


def test_renderer_keeps_duplicates_spaces_and_numbering():
    report = TranslationReport(input_path=Path("book.epub"), detected_language="en", target_language="pt",
        missing_texts=["  Café λ HTTP  ", "Same", "Same"])
    assert reports._render_missing_texts(report) == expected(Path("book.epub"), report.missing_texts)
