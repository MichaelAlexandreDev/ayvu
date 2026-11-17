from tests.support.cli_rich_runtime_support import declare_model
import json
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.translation.preflight import PreflightError
from ayvu.recovery.checkpoint import ResumeStateStore
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import FakeCache, _fake_translation
from tests.support.cli_support import isolated_config, runner


def test_translate_command_stops_when_preflight_fails(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")

    def fail_preflight(**_kwargs: object) -> object:
        raise PreflightError("Cache check failed: no write permission", "Choose a writable cache path.")

    def fail_translate(*_args: object, **_kwargs: object) -> TranslationReport:
        raise AssertionError("translation should not start when preflight fails")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)
    _fake_translation(monkeypatch, fail_translate)

    declare_model(isolated_config)
    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    assert result.exit_code == 1
    assert "Default output folder:" in result.output
    assert "Keep this output location?" in result.output
    assert "Cache check failed: no write permission" in result.output
    assert "Choose a writable cache path." in result.output
    assert "Traceback" not in result.output


def test_translate_command_uses_configured_output_and_processing_dirs(isolated_config, tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    books_dir = tmp_path / "Biblioteca"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, Path] = {}
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {
                    "translated": "PT",
                    "processing": "Em-Andamento",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_translate(input_path: Path, output_path: Path, **_kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
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
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    output_path = books_dir / "PT" / "book-pt.epub"
    processing_dir = books_dir / "Em-Andamento"
    state_path = processing_dir / "book-pt.ayvu-state.json"
    resume_state = ResumeStateStore(processing_dir).load(state_path)
    assert result.exit_code == 0
    assert str(output_path.parent) in result.output.replace("\n", "")
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_path
    assert resume_state.output_path == output_path.resolve()


def test_translate_command_uses_output_dir_for_single_epub(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "book.epub"
    output_dir = tmp_path / "translated"
    processing_dir = tmp_path / "processing"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, Path] = {}

    def fake_translate(input_path: Path, output_path: Path, **_kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
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
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_dir / "book-pt.epub"
    assert "Keep this output location?" not in result.output
