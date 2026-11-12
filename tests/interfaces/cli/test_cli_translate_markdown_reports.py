from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.v1.epub import TranslationReport
from ayvu.v1.resume import COMPLETED_STATUS, ResumeStateStore
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import FakeCache, _fake_translation
from tests.support.cli_support import isolated_config, runner


def test_translate_command_offers_and_saves_markdown_report(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    reports_dir = tmp_path / "reports"
    processing_dir = tmp_path / "processing"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_translated=2,
        texts_from_cache=1,
        output_path=output_path,
        input_path=epub_path,
        detected_language="en",
        target_language="pt",
    )
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._default_reports_dir", lambda: reports_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path), "--output", str(output_path)], input="y\n")

    report_path = reports_dir / "book-pt-report.md"
    state_path = processing_dir / "book-pt.ayvu-state.json"
    resume_state = ResumeStateStore(processing_dir).load(state_path)
    assert result.exit_code == 0
    assert "Translation report" in result.output
    assert "Original EPUB" in result.output
    assert "Detected language" in result.output
    assert "Save translation report as Markdown?" in result.output
    assert "Report saved to:" in result.output
    assert report_path.exists()
    assert "- Original EPUB: " in report_path.read_text(encoding="utf-8")
    assert str(epub_path) in report_path.read_text(encoding="utf-8")
    assert resume_state.status == COMPLETED_STATUS
    assert resume_state.input_path == epub_path.resolve()
    assert resume_state.output_path == output_path.resolve()
    assert resume_state.cache_path == Path(".cache/traducoes.sqlite").resolve()
    assert resume_state.source == "en"
    assert resume_state.target == "pt"
    assert resume_state.translator_name == "libretranslate"
    assert not resume_state.overwrite


def test_translate_command_puts_validation_warnings_in_report_and_markdown(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    reports_dir = tmp_path / "reports"
    processing_dir = tmp_path / "processing"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_translated=2,
        texts_from_cache=1,
        output_path=output_path,
        input_path=epub_path,
        detected_language="en",
        target_language="pt",
    )
    warning = "Capítulo sem texto visível: text/empty.xhtml"
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=False, warnings=[warning], document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._default_reports_dir", lambda: reports_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(
        app, ["--mode", "common", "translate", str(epub_path), "--output", str(output_path)], input="y\n"
    )

    report_path = reports_dir / "book-pt-report.md"
    markdown = report_path.read_text(encoding="utf-8")
    assert result.exit_code == 1
    assert "Validation warnings" in result.output
    assert warning in result.output
    assert report_path.exists()
    assert "## Validation warnings" in markdown
    assert warning in markdown
    assert "Traceback" not in result.output
