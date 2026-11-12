"""Tests for the extracted resume command."""
from pathlib import Path
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.recovery.checkpoint import ResumeStateStore, TranslationResumeState
from ayvu.formats.epub.validation import ValidationResult
from ayvu.kernel import LanguagePair, TranslationOptions
from tests.support.cli_support import FakeCache, _fake_translation, runner
from tests.support.cli_support import isolated_config


def _running_resume_state(
    tmp_path: Path,
    stem: str,
    target: str,
    create_epub: bool = True,
    workers: int = 1,
    requests_per_second: float | None = None,
    retries: int = 2,
    retry_backoff: float = 0.5,
    retry_backoff_max: float = 8.0,
) -> TranslationResumeState:
    epub_path = tmp_path / "Original" / f"{stem}.epub"
    if create_epub:
        epub_path.parent.mkdir(parents=True, exist_ok=True)
        epub_path.write_bytes(b"fake epub")
    return TranslationResumeState.create(
        input_path=epub_path,
        output_path=tmp_path / "Traduzidos" / f"{stem}-{target}.epub",
        cache_path=tmp_path / "cache.sqlite",
        translator_name="libretranslate",
        url="http://localhost:5000",
        glossary_path=None,
        options=TranslationOptions(
            language_pair=LanguagePair(source="en", target=target),
            workers=workers,
        ),
        overwrite=False,
        timeout=30.0,
        retries=retries,
        requests_per_second=requests_per_second,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
    )


def _patch_resume_pipeline(monkeypatch, processing_dir: Path, calls: dict[str, object]) -> None:
    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        options = kwargs["options"]
        calls["options"] = options
        return TranslationReport(
            output_path=output_path,
            input_path=input_path,
            target_language=options.target,
        )

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


def test_resume_command_resumes_single_state(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    state = _running_resume_state(
        tmp_path,
        "book",
        "pt",
        workers=2,
        requests_per_second=2.5,
        retries=4,
        retry_backoff=1.0,
        retry_backoff_max=10.0,
    ).record_chapter("chapter-one.xhtml", 2, ok=True)
    ResumeStateStore(processing_dir).save(state)
    calls: dict[str, object] = {}
    _patch_resume_pipeline(monkeypatch, processing_dir, calls)

    result = runner.invoke(app, ["resume"])

    assert result.exit_code == 0
    assert "Resuming translation:" in result.output
    assert "Resume checkpoint" in result.output
    assert "1/2" in result.output
    assert "Workers" in result.output
    assert "2 (parallel" in result.output
    assert "2.5 requests/second" in result.output
    assert "shared across workers" in result.output
    assert "4 retries" in result.output
    assert calls["options"].source == "en"
    assert calls["options"].target == "pt"
    assert calls["options"].workers == 2


def test_resume_command_without_state_reports_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, ["resume"])

    assert result.exit_code == 1
    assert "No translation in progress was found to resume." in result.output
    assert "ayvu translate" in result.output
    assert "Traceback" not in result.output


def test_resume_command_selects_state_by_epub_and_target(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    store = ResumeStateStore(processing_dir)
    store.save(_running_resume_state(tmp_path, "alpha", "pt"))
    beta = _running_resume_state(tmp_path, "beta", "es")
    store.save(beta)
    calls: dict[str, object] = {}
    _patch_resume_pipeline(monkeypatch, processing_dir, calls)

    result = runner.invoke(app, ["resume", str(beta.input_path), "--target", "es"])

    assert result.exit_code == 0
    assert "beta.epub" in result.output
    assert calls["options"].target == "es"


def test_resume_command_requires_disambiguation_for_multiple_states(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    store = ResumeStateStore(processing_dir)
    store.save(_running_resume_state(tmp_path, "alpha", "pt"))
    store.save(_running_resume_state(tmp_path, "beta", "es"))
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, ["resume"])

    assert result.exit_code == 1
    assert "Several translations in progress match the request." in result.output
    assert "Specify the EPUB and --target" in result.output
    assert "Traceback" not in result.output
