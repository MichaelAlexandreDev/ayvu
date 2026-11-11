import json
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.options import DEFAULT_PREVIEW_DOCUMENT_LIMIT
from ayvu.interfaces.cli.app import app
from ayvu.v1.epub import TranslationReport
from ayvu.providers.libretranslate.client import TranslatorLanguage
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import FakeCache, _fake_translation, isolated_config, runner


def test_root_command_generates_guided_preview_when_confirmed(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    preview_dir = tmp_path / "Preview"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr(
        "ayvu.composition.builders.providers.LibreTranslateTranslator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("languages should not be listed")),
    )
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, [], input=f"2\n{epub_path}\n1\n")

    options = calls["options"]
    assert result.exit_code == 0
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "EPUB path" in result.output
    assert "Default target language:" in result.output
    assert "Use default target language (pt)" in result.output
    assert "Outro idioma" in result.output
    assert "Choose target language" in result.output
    assert "Preview output folder:" in result.output
    assert "Preview EPUB name:" in result.output
    assert "Preview salvo em:" in result.output
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == preview_dir / "book-preview.epub"
    assert options.max_documents == DEFAULT_PREVIEW_DOCUMENT_LIMIT
    assert "Usage:" not in result.output


def test_root_command_allows_guided_preview_target_from_languages(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    preview_dir = tmp_path / "Preview"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    class FakeLanguageTranslator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def list_languages(self) -> tuple[TranslatorLanguage, ...]:
            return (
                TranslatorLanguage(code="pt", name="Portuguese", targets=("en",)),
                TranslatorLanguage(code="es", name="Spanish", targets=("en",)),
            )

    def fake_preflight(**kwargs: object) -> object:
        calls["target"] = kwargs["language_pair"].target
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(_input_path: Path, _output_path: Path, **kwargs: object) -> TranslationReport:
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=_output_path, input_path=_input_path, target_language="es")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FakeLanguageTranslator)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, [], input=f"2\n{epub_path}\n2\n2\n")

    options = calls["options"]
    assert result.exit_code == 0
    assert "Default target language:" in result.output
    assert "Outro idioma" in result.output
    assert "LibreTranslate languages" in result.output
    assert "Option" in result.output
    assert "Portuguese" in result.output
    assert "Spanish" in result.output
    assert "Target language option or code" in result.output
    assert calls["target"] == "es"
    assert options.target == "es"


def test_root_command_starts_guided_translation(tmp_path, monkeypatch):
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

    result = runner.invoke(app, [], input=f"1\n{epub_path}\n1\ny\n")

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

    result = runner.invoke(app, [], input=f"1\n{epub_path}\n1\n1\ny\n")

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

    result = runner.invoke(app, [], input=f"1\n{epub_path}\n1\n1\ny\n")

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
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "es"}) + "\n",
        encoding="utf-8",
    )
    epub_path = tmp_path / "book.epub"
    preview_dir = tmp_path / "Preview"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> object:
        calls["target"] = kwargs["language_pair"].target
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(_input_path: Path, _output_path: Path, **kwargs: object) -> TranslationReport:
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=_output_path, input_path=_input_path, target_language="es")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, [], input=f"2\n{epub_path}\n1\n")

    assert result.exit_code == 0
    assert "Default target language:" in result.output
    assert calls["target"] == "es"
    assert calls["options"].target == "es"
