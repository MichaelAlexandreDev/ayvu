from tests.support.cli_rich_runtime_support import Transport, declare_model
from tests.support.cli_translate_project_support import _book
from tests.support.workflow_characterization_support import FakeLibreTranslateSession
import json
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.options import DEFAULT_PREVIEW_DOCUMENT_LIMIT
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.providers.libretranslate.client import TranslatorLanguage
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import FakeCache, _fake_translation, isolated_config, runner


def test_root_command_generates_guided_preview_when_confirmed(isolated_config, tmp_path, monkeypatch):
    declare_model(isolated_config)
    epub_path = _book(tmp_path / "book.epub")
    preview_dir = tmp_path / "Preview"
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)

    result = runner.invoke(app, [], input=f"2\n\n{epub_path}\n1\n")

    assert result.exit_code == 0, result.output
    for message in ("Choose an option", "Generate preview", "EPUB path", "Default target language:",
                    "Use default target language (pt)", "Outro idioma", "Choose target language",
                    "Preview output folder:", "Preview EPUB name:", "Preview salvo em:"):
        assert message in result.output
    assert "Usage:" not in result.output
    assert (preview_dir / "book-preview.epub").is_file()
    assert session.posts == ["T", "Hello title", "Hello reader.", "Goodbye reader."]
    assert all(payload["target"] == "pt" for method, _, payload in session.requests if method == "POST")
    assert not (tmp_path / "missing").exists()


def test_root_command_allows_guided_preview_target_from_languages(isolated_config, tmp_path, monkeypatch):
    declare_model(isolated_config)
    epub_path = _book(tmp_path / "book.epub")
    preview_dir = tmp_path / "Preview"
    session = FakeLibreTranslateSession([
        {"code": "en", "name": "English", "targets": ["pt", "es"]},
        {"code": "pt", "name": "Portuguese", "targets": ["en"]},
        {"code": "es", "name": "Spanish", "targets": ["en"]},
    ])
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)

    result = runner.invoke(app, [], input=f"2\n\n{epub_path}\n2\nes\n")

    assert result.exit_code == 0, result.output
    for message in ("Default target language:", "Outro idioma", "LibreTranslate languages", "Option",
                    "Portuguese", "Spanish", "Target language option or code"):
        assert message in result.output
    assert session.posts and all(payload["target"] == "es" for payload in session.posts)
    assert (preview_dir / "book-preview.epub").is_file()


def test_root_command_starts_guided_translation(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "book.epub"
    output_dir = tmp_path / "Traduzidos"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
    result = runner.invoke(app, [], input=f"1\n{epub_path}\n\n1\ny\n")

    options = calls["options"]
    assert result.exit_code == 0
    assert "Translate a book" in result.output
    assert "EPUB path" in result.output
    assert "Default target language:" in result.output
    assert "Outro idioma" in result.output
    assert "Default output folder:" in result.output
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_dir / "book-pt.epub"
    assert options.target == "pt"


def test_root_command_selects_saved_glossary_for_guided_translation(isolated_config, tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_dir = tmp_path / "Traduzidos"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")
    glossary_dir = isolated_config.parent / "glossaries"
    glossary_dir.mkdir()
    glossary_path = glossary_dir / "technical.json"
    glossary_path.write_text('{"Observer": {"rule": "preserve"}}\n', encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=object(), route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["glossary"] = kwargs["glossary"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
    result = runner.invoke(app, [], input=f"1\n{epub_path}\n\n1\n1\ny\n")

    assert result.exit_code == 0
    assert "Saved glossaries are available." in result.output
    assert "Glossary selected:" in result.output
    assert calls["preflight"]["glossary_path"] == glossary_path
    assert calls["glossary"] is not None


def test_root_command_selects_profile_for_guided_translation(isolated_config, tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_dir = tmp_path / "Traduzidos"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")
    profile_glossary_path = isolated_config.parent / "glossaries" / "technical.json"
    profile_glossary_path.parent.mkdir(parents=True)
    profile_glossary_path.write_text('{}')
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "profiles": {
                    "technical": {
                        "target_language": "es",
                        "glossary": "technical.json",
                        "style": "technical",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=object(), route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="es")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
    result = runner.invoke(app, [], input=f"1\n{epub_path}\n1\n\n1\ny\n")

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert "Translation profiles are configured." in result.output
    assert "Profile selected:" in result.output
    assert "Profile glossary:" in result.output
    assert preflight["language_pair"].target == "es"
    assert preflight["glossary_path"] == profile_glossary_path
    assert options.target == "es"


def test_root_command_uses_saved_default_language_in_guided_preview(isolated_config, tmp_path, monkeypatch):
    declare_model(isolated_config, default_target_language="es")
    epub_path = _book(tmp_path / "book.epub")
    preview_dir = tmp_path / "Preview"
    session = FakeLibreTranslateSession([
        {"code": "en", "name": "English", "targets": ["es"]},
        {"code": "es", "name": "Spanish", "targets": ["en"]},
    ])
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)

    result = runner.invoke(app, [], input=f"2\n\n{epub_path}\n1\n")

    assert result.exit_code == 0, result.output
    assert "Default target language:" in result.output
    assert session.posts and all(payload["target"] == "es" for payload in session.posts)
    assert (preview_dir / "book-preview.epub").is_file()
