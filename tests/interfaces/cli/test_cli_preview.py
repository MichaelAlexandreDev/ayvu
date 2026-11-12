"""Tests for the extracted CLI translation workflow."""
import json
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.options import DEFAULT_PREVIEW_DOCUMENT_LIMIT
from ayvu.interfaces.cli.app import app
from ayvu.v1.epub import TranslationReport
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import isolated_config, FakeCache, _fake_translation, runner


def test_preview_option_generates_preview_with_default_settings(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    preview_dir = tmp_path / "Preview"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, ["--preview", str(epub_path)])

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert "Preview output folder:" in result.output
    assert str(preview_dir) in result.output
    assert "book-preview.epub" in result.output
    assert "Preview salvo em:" in result.output
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == preview_dir / "book-preview.epub"
    assert preflight["epub_path"] == epub_path
    assert preflight["cache_path"] == Path(".cache/traducoes.sqlite")
    assert preflight["translator_name"] == "libretranslate"
    assert preflight["url"] == "http://localhost:5000"
    assert options.source == "en"
    assert options.target == "pt"
    assert options.max_documents == DEFAULT_PREVIEW_DOCUMENT_LIMIT


def test_preview_option_uses_configured_preview_dir(isolated_config, tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    books_dir = tmp_path / "Biblioteca"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"preview": "Amostras"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )

    result = runner.invoke(app, ["--preview", str(epub_path)])

    output_path = books_dir / "Amostras" / "book-preview.epub"
    assert result.exit_code == 0
    assert str(output_path.parent) in result.output.replace("\n", "")
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_path
    assert calls["options"].max_documents == DEFAULT_PREVIEW_DOCUMENT_LIMIT
