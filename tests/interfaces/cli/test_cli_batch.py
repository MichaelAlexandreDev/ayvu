"""Tests for the extracted CLI translation workflow."""
from tests.support.cli_rich_runtime_support import declare_model
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.translation.preflight import PreflightError
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import isolated_config, FakeCache, _fake_translation, runner


def test_translate_command_batch_writes_outputs_and_markdown_reports(tmp_path, monkeypatch, isolated_config):
    first_epub = tmp_path / "first.epub"
    second_epub = tmp_path / "second.epub"
    output_dir = tmp_path / "translated"
    reports_dir = tmp_path / "reports"
    processing_dir = tmp_path / "processing"
    first_epub.write_bytes(b"fake epub")
    second_epub.write_bytes(b"fake epub")
    calls: list[tuple[Path, Path]] = []

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls.append((input_path, output_path))
        return TranslationReport(
            output_path=output_path,
            input_path=input_path,
            detected_language=kwargs["options"].source,
            target_language=kwargs["options"].target,
            chapters_processed=1,
            texts_translated=2,
        )

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.report._reports_dir", lambda _config: reports_dir)
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

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        [
            "translate",
            str(first_epub),
            str(second_epub),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (first_epub, output_dir / "first-pt.epub"),
        (second_epub, output_dir / "second-pt.epub"),
    ]
    assert "Batch translation plan" in result.output
    assert "Batch translation summary" in result.output
    assert "Report saved to:" in result.output
    assert (reports_dir / "first-pt-report.md").exists()
    assert (reports_dir / "second-pt-report.md").exists()
    assert "- Original EPUB: " in (reports_dir / "first-pt-report.md").read_text(encoding="utf-8")


def test_translate_command_batch_rejects_single_output_path(tmp_path):
    first_epub = tmp_path / "first.epub"
    second_epub = tmp_path / "second.epub"
    first_epub.write_bytes(b"fake epub")
    second_epub.write_bytes(b"fake epub")

    result = runner.invoke(
        app,
        [
            "translate",
            str(first_epub),
            str(second_epub),
            "--output",
            str(tmp_path / "book-pt.epub"),
        ],
    )

    assert result.exit_code == 1
    assert "Não é possível usar --output com múltiplos EPUBs." in result.output
    assert "--output-dir" in result.output
    assert "Traceback" not in result.output


def test_translate_command_batch_rejects_review_output(tmp_path):
    first_epub = tmp_path / "first.epub"
    second_epub = tmp_path / "second.epub"
    first_epub.write_bytes(b"fake epub")
    second_epub.write_bytes(b"fake epub")

    result = runner.invoke(
        app,
        [
            "translate",
            str(first_epub),
            str(second_epub),
            "--review-output",
            str(tmp_path / "review.csv"),
        ],
    )

    assert result.exit_code == 1
    assert "Não é possível usar --review-output com múltiplos EPUBs." in result.output
    assert "Traceback" not in result.output


def test_translate_command_batch_rejects_existing_output_without_overwrite(tmp_path, monkeypatch, isolated_config):
    first_epub = tmp_path / "first.epub"
    second_epub = tmp_path / "second.epub"
    output_dir = tmp_path / "translated"
    existing_output = output_dir / "first-pt.epub"
    first_epub.write_bytes(b"fake epub")
    second_epub.write_bytes(b"fake epub")
    output_dir.mkdir()
    existing_output.write_text("already here", encoding="utf-8")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run when a batch output already exists")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        [
            "translate",
            str(first_epub),
            str(second_epub),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 1
    assert "existing output was not changed." in result.output
    assert "Batch translation summary" not in result.output
    assert "Batch item 1/2" not in result.output
    assert existing_output.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_batch_continues_after_failure_when_requested(tmp_path, monkeypatch, isolated_config):
    first_epub = tmp_path / "first.epub"
    second_epub = tmp_path / "second.epub"
    output_dir = tmp_path / "translated"
    reports_dir = tmp_path / "reports"
    processing_dir = tmp_path / "processing"
    first_epub.write_bytes(b"fake epub")
    second_epub.write_bytes(b"fake epub")
    translated_inputs: list[Path] = []

    def fake_preflight(**kwargs: object) -> object:
        if kwargs["epub_path"] == first_epub:
            raise PreflightError(
                "Não foi possível preparar o tradutor.",
                "Verifique o tradutor local e tente novamente.",
                detail="first failed",
            )
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        translated_inputs.append(input_path)
        return TranslationReport(
            output_path=output_path,
            input_path=input_path,
            detected_language=kwargs["options"].source,
            target_language=kwargs["options"].target,
        )

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.report._reports_dir", lambda _config: reports_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        [
            "translate",
            str(first_epub),
            str(second_epub),
            "--output-dir",
            str(output_dir),
            "--continue-on-error",
        ],
    )

    assert result.exit_code == 1
    assert translated_inputs == [second_epub]
    assert "Não foi possível preparar o tradutor." in result.output
    assert "Batch item failed:" in result.output
    assert "Batch translation summary" in result.output
    assert "Failed" in result.output
    assert "OK" in result.output
    assert not (reports_dir / "first-pt-report.md").exists()
    assert (reports_dir / "second-pt-report.md").exists()
    assert "Traceback" not in result.output
