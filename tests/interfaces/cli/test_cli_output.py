from test_cli import isolated_config
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.v1.epub import TranslationReport
from ayvu.v1.resume import ResumeStateStore
from ayvu.formats.epub.validation import ValidationResult
from test_cli import FakeCache, _fake_translation, runner


def test_translate_command_confirms_default_output_location(tmp_path, monkeypatch):
    original_dir = tmp_path / "Original"
    epub_path = original_dir / "book.epub"
    output_dir = tmp_path / "Traduzidos"
    processing_dir = tmp_path / "Processando"
    epub_path.parent.mkdir()
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, Path] = {}

    def fake_translate(input_path: Path, output_path: Path, **_kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        return TranslationReport(
            output_path=output_path,
            input_path=input_path,
            detected_language="en",
            target_language="pt",
        )

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.interfaces.cli.app.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    output_path = output_dir / "book-pt.epub"
    state_path = processing_dir / "book-pt.ayvu-state.json"
    resume_state = ResumeStateStore(processing_dir).load(state_path)
    assert result.exit_code == 0
    assert "Default output folder:" in result.output
    assert str(output_dir) in result.output
    assert "Translated EPUB name:" in result.output
    assert "book-pt.epub" in result.output
    assert "Original EPUB stays in Original:" in result.output
    assert "Keep this output location?" in result.output
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_path
    assert resume_state.output_path == output_path.resolve()


def test_translate_command_allows_custom_output_path_from_default_prompt(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_dir = tmp_path / "Traduzidos"
    custom_output = tmp_path / "Escolhidos" / "custom-name"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, Path] = {}

    def fake_translate(_input_path: Path, output_path: Path, **_kwargs: object) -> TranslationReport:
        calls["output_path"] = output_path
        return TranslationReport(output_path=output_path, input_path=epub_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.interfaces.cli.app.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input=f"n\n{custom_output}\n")

    output_path = custom_output.with_suffix(".epub")
    assert result.exit_code == 0
    assert "Keep this output location?" in result.output
    assert "Output EPUB path" in result.output
    assert calls["output_path"] == output_path


def test_translate_command_asks_how_to_handle_existing_output_and_cancels(tmp_path):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    epub_path.write_bytes(b"not a real epub")
    output_path.write_text("already here", encoding="utf-8")

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path), "--output", str(output_path)], input="0\n")

    assert result.exit_code == 1
    assert "Output path:" in result.output
    assert str(output_path) in result.output
    assert "Translated EPUB already exists." in result.output
    assert "Overwrite existing EPUB" in result.output
    assert "Choose another name" in result.output
    assert "Cancel" in result.output
    assert "Canceled:" in result.output
    assert "existing output was not changed." in result.output
    assert output_path.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_allows_another_name_when_output_exists(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    renamed_output = tmp_path / "book-custom"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")
    output_path.write_text("already here", encoding="utf-8")
    calls: dict[str, Path] = {}

    def fake_translate(_input_path: Path, output_path: Path, **_kwargs: object) -> TranslationReport:
        calls["output_path"] = output_path
        return TranslationReport(output_path=output_path, input_path=epub_path, target_language="pt")

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.interfaces.cli.app.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output", str(output_path)],
        input=f"2\n{renamed_output}\n",
    )

    final_output = renamed_output.with_suffix(".epub")
    assert result.exit_code == 0
    assert "Translated EPUB already exists." in result.output
    assert "Choose another name" in result.output
    assert "Output EPUB path" in result.output
    assert "Final output path:" in result.output
    assert str(final_output) in result.output
    assert calls["output_path"] == final_output
    assert output_path.read_text(encoding="utf-8") == "already here"


def test_translate_command_rejects_another_existing_output_name(tmp_path):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    other_output = tmp_path / "already-used.epub"
    epub_path.write_bytes(b"not a real epub")
    output_path.write_text("already here", encoding="utf-8")
    other_output.write_text("also here", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output", str(output_path)],
        input=f"2\n{other_output}\nn\n",
    )

    assert result.exit_code == 1
    assert "Output path already exists:" in result.output
    assert str(other_output) in result.output
    assert "Choose another output path?" in result.output
    assert "existing output was not changed." in result.output
    assert output_path.read_text(encoding="utf-8") == "already here"
    assert other_output.read_text(encoding="utf-8") == "also here"
    assert "Traceback" not in result.output


def test_translate_command_developer_mode_requires_overwrite_for_existing_output(tmp_path):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    epub_path.write_bytes(b"not a real epub")
    output_path.write_text("already here", encoding="utf-8")

    result = runner.invoke(app, ["translate", str(epub_path), "--output", str(output_path)])

    assert result.exit_code == 1
    assert "existing output was not changed." in result.output
    assert "Use --overwrite to replace it or --output to choose another EPUB path." in result.output
    assert "Choose an option" not in result.output
    assert output_path.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_continues_when_existing_output_is_confirmed(tmp_path):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    epub_path.write_bytes(b"not a real epub")
    output_path.write_text("already here", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output", str(output_path), "--translator", "unknown"],
        input="1\n",
    )

    assert result.exit_code == 1
    assert "Overwrite existing EPUB" in result.output
    assert "Não foi possível preparar o tradutor." in result.output
    assert "Use --translator libretranslate." in result.output
    assert "Detalhe técnico:" not in result.output
    assert "Unsupported translator:" not in result.output
    assert output_path.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output
