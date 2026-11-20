from tests.support.cli_rich_runtime_support import declare_model
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.translation import HtmlTranslationStats
from ayvu.translation.preflight import PreflightError
from ayvu.recovery.checkpoint import COMPLETED_STATUS, ResumeStateStore
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import _fake_translation
from tests.support.cli_support import _resume_state, isolated_config, runner


def test_root_command_refuses_a_v1_translation_without_offering_it(tmp_path, monkeypatch):
    # REF-18 step 6: the offer shows a v1 checkpoint and refuses it; nothing runs and the menu follows.
    processing_dir = tmp_path / "Processando"
    path = ResumeStateStore(processing_dir).save(_resume_state(tmp_path))
    before = path.read_bytes()
    calls: dict[str, object] = {}
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", lambda **kwargs: calls.setdefault("preflight", kwargs))
    _fake_translation(monkeypatch, lambda *_args, **kwargs: calls.setdefault("translation", kwargs))

    result = runner.invoke(app, [], input="0\n")

    output = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Resume checkpoint" in output and "previous (v1)" in output
    assert "Continue detected translation?" not in output and "Resuming translation:" not in output
    assert "This job started on Ayvu's previous runtime, which this version no longer runs." in output
    assert "Finish it with the frozen release v1-runtime-final" in output and "Traceback" not in output
    assert "cached translations are reused" not in output
    assert calls == {} and path.read_bytes() == before


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


def test_translate_command_records_chapter_checkpoint(tmp_path, monkeypatch, isolated_config):
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
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    declare_model(isolated_config)
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
