from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.translation import HtmlTranslationStats
from ayvu.translation.preflight import PreflightError
from ayvu.recovery.checkpoint import COMPLETED_STATUS, ResumeStateStore
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import FakeCache, _fake_translation
from tests.support.cli_support import _resume_state, isolated_config, runner


def test_root_command_resumes_detected_translation_when_confirmed(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    state = _resume_state(tmp_path)
    report = TranslationReport(
        chapters_processed=1,
        texts_translated=2,
        output_path=state.output_path,
        input_path=state.input_path,
        detected_language=state.source,
        target_language=state.target,
    )
    calls: dict[str, object] = {}
    ResumeStateStore(processing_dir).save(state)

    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(*_args: object, **kwargs: object) -> TranslationReport:
        calls["translation_options"] = kwargs["options"]
        return report

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, [], input="y\n")

    preflight = calls["preflight"]
    options = calls["translation_options"]
    saved_state = ResumeStateStore(processing_dir).load(processing_dir / "book-pt.ayvu-state.json")
    assert result.exit_code == 0
    assert "Continue detected translation?" in result.output
    assert "Resuming translation:" in result.output
    assert "Translation report" in result.output
    assert "Usage:" not in result.output
    assert preflight["epub_path"] == state.input_path
    assert preflight["cache_path"] == state.cache_path
    assert preflight["glossary_path"] == state.glossary_path
    assert preflight["translator_name"] == state.translator_name
    assert preflight["url"] == state.url
    assert preflight["timeout"] == state.timeout
    assert preflight["retries"] == state.retries
    assert options.source == state.source
    assert options.target == state.target
    assert options.chunk_limit == state.chunk_limit
    assert options.translate_metadata == state.translate_metadata
    assert saved_state.status == COMPLETED_STATUS


def test_root_command_reports_resume_failure_without_traceback(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    ResumeStateStore(processing_dir).save(_resume_state(tmp_path))
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreflightError("EPUB check failed: missing file", "Choose a valid EPUB.")
        ),
    )

    result = runner.invoke(app, [], input="y\n")

    assert result.exit_code == 1
    assert "Continue detected translation?" in result.output
    assert "EPUB check failed: missing file" in result.output
    assert "Choose a valid EPUB." in result.output
    assert "Não foi possível retomar a tradução detectada." in result.output
    assert "Traceback" not in result.output


def test_root_command_reports_invalid_processing_state(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    processing_dir.mkdir()
    (processing_dir / "bad.ayvu-state.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, [], input="n\n")

    assert result.exit_code == 0
    assert "Invalid processing state files were found." in result.output
    assert "bad.ayvu-state.json" in result.output
    assert "not valid JSON" in result.output
    assert "Restart the translation" in result.output
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "Usage:" in result.output


def test_root_command_without_processing_state_has_no_processing_noise(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "Canceled." in result.output
    assert "Translations in progress were found." not in result.output
    assert "Invalid processing state files were found." not in result.output


def test_translate_command_records_chapter_checkpoint(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    cache_path = tmp_path / "cache.sqlite"
    processing_dir = tmp_path / "Processando"
    epub_path.write_bytes(b"fake epub")

    def fake_translate(*_args: object, **kwargs: object) -> TranslationReport:
        kwargs["on_chapter_start"](1, 2, "chapter-one.xhtml")
        kwargs["on_chapter_done"](1, 2, "chapter-one.xhtml", HtmlTranslationStats())
        kwargs["on_chapter_start"](2, 2, "chapter-two.xhtml")
        kwargs["on_chapter_done"](
            2, 2, "chapter-two.xhtml", HtmlTranslationStats(errors=["boom"], missing=1)
        )
        return TranslationReport(output_path=output_path, input_path=epub_path, target_language="pt")

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

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache",
            str(cache_path),
            "--source",
            "en",
            "--target",
            "pt",
        ],
    )

    saved = ResumeStateStore(processing_dir).load(processing_dir / "book-pt.ayvu-state.json")
    assert result.exit_code == 0
    assert saved.completed_chapters == ("chapter-one.xhtml",)
    assert saved.failed_chapters == ("chapter-two.xhtml",)
    assert saved.failed_segment_count == 2
    assert saved.total_chapters == 2
