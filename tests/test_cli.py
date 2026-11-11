import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
from ebooklib import epub
from typer.testing import CliRunner
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.interfaces.cli.app import app
from ayvu.composition.cli import _BookOutcome
from ayvu.v1.epub import TranslationReport
from ayvu.jobs.model import JobState
from ayvu.v1.html_translate import HtmlTranslationStats
from ayvu.translation.preflight import PreflightError
from ayvu.v1.resume import COMPLETED_STATUS, ResumeStateStore, TranslationResumeState
from ayvu.review.application import ReviewExportResult, ReviewOutcome, ReviewPackageIdentity
from ayvu.review.csv_export import ReviewSegment, write_review_csv
from ayvu.translation.engine import EngineResult
from ayvu.translation.project_contracts import OutputValidation, TranslateProjectResult
from ayvu.providers.libretranslate.client import TranslatorError, TranslatorLanguage
from ayvu.formats.epub.validation import ValidationResult
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.interfaces.cli import run_support as cli_run_support


runner = CliRunner()


def _fake_translation(monkeypatch, fake_translate) -> None:
    """Replaces the book's work where both flows meet, to test what the CLI does around it.

    The real run through the use case is in tests/test_workflow_characterization.py and tests/test_cli_translate_project.py.
    """
    def run(book, hooks):
        preflight = book.prepare()
        report = fake_translate(
            book.epub_path, book.output_plan.path, translator=preflight.translator, options=book.options, glossary=preflight.glossary,
            on_chapter_start=hooks.on_chapter_start, on_chapter_done=hooks.on_chapter_done, on_text_processed=hooks.on_text_processed,
            translator_factory=getattr(preflight, "translator_factory", None))
        validation = cli_run_support.validate_output(book.output_plan.path, on_progress=hooks.on_validation) if report.output_written else None
        return _BookOutcome(report=report, validation=validation)

    monkeypatch.setattr("ayvu.interfaces.cli.run_support._translate_in_project", run)
    monkeypatch.setattr("ayvu.composition.cli.translate_without_project", fake_translate)


def _seed_cache_entry(
    cache_path: Path,
    text: str,
    translated: str,
    *,
    source: str = "en",
    target: str = "pt",
    created_at: str = "2024-01-01 10:00:00",
) -> CacheKey:
    key = CacheKey(text=text, language_pair=LanguagePair(source=source, target=target))
    with TranslationCache(cache_path) as cache:
        cache.set(key, translated)
        cache.connection.execute(
            """
            UPDATE translations
            SET created_at = ?
            WHERE source_lang = ?
              AND target_lang = ?
              AND original_text_hash = ?
            """,
            (created_at, source, target, key.original_text_hash),
        )
        cache.connection.commit()
    return key


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Isolate Ayvu config per test and pre-write a default config.

    Most guided-flow tests assume the default language already exists, so the
    first-use prompt does not fire. Tests that exercise the first-use flow
    delete the returned path before invoking the CLI.
    """
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "Processando")
    config_path = config_home / "ayvu" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"version": 1, "default_target_language": "pt"}) + "\n",
        encoding="utf-8",
    )
    return config_path


class FakeNoLanguagesTranslator:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        raise TranslatorError("no server")


def test_output_plan_keeps_explicit_output():
    output = Path("traduzidos/livro-final.epub")
    language_pair = LanguagePair(source="en", target="pt")

    plan = OutputPlan.for_translation(Path("livro.epub"), output, language_pair)

    assert plan.path == output
    assert plan.explicit_output


def test_output_plan_uses_target_suffix_in_default_output_dir():
    language_pair = LanguagePair(source="en", target="pt-BR")
    default_dir = Path("Documentos/Livros/Traduzidos")

    plan = OutputPlan.for_translation(
        Path("books/livro.epub"),
        None,
        language_pair,
        default_dir=default_dir,
    )

    assert plan.path == Path("Documentos/Livros/Traduzidos/livro-pt-BR.epub")
    assert not plan.explicit_output


def test_output_plan_uses_translated_suffix_when_target_is_blank():
    language_pair = LanguagePair(source="en", target=" ")
    default_dir = Path("Documentos/Livros/Traduzidos")

    plan = OutputPlan.for_translation(
        Path("books/livro.epub"),
        None,
        language_pair,
        default_dir=default_dir,
    )

    assert plan.path == Path("Documentos/Livros/Traduzidos/livro-translated.epub")


def test_output_plan_dry_run_does_not_block_existing_output(tmp_path):
    output = tmp_path / "livro-pt.epub"
    output.write_text("already here", encoding="utf-8")

    plan = OutputPlan(path=output, dry_run=True)

    assert not plan.blocks_existing_file(overwrite=False)


def test_output_plan_uses_preview_suffix_in_default_preview_dir():
    default_dir = Path("Documentos/Livros/Preview")

    plan = OutputPlan.for_preview(Path("books/livro.epub"), default_dir=default_dir)

    assert plan.path == Path("Documentos/Livros/Preview/livro-preview.epub")
    assert not plan.explicit_output


def test_translation_options_exposes_language_pair_values():
    language_pair = LanguagePair(source="en", target="pt")

    options = TranslationOptions(language_pair=language_pair)

    assert options.source == "en"
    assert options.target == "pt"


def test_root_command_shows_processing_translation_state(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    state = _resume_state(tmp_path)
    ResumeStateStore(processing_dir).save(state)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, [], input="n\n0\n")

    assert result.exit_code == 0
    assert "Translations in progress were found." in result.output
    assert "Processing translations" in result.output
    assert "book.epub" in result.output
    assert "book-pt.epub" in result.output
    assert "cache.sqlite" in result.output
    assert "Execution" in result.output
    assert "1 (sequential)" in result.output
    assert "Continue detected translation?" in result.output
    assert "Detected translation was not resumed." in result.output
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "Canceled." in result.output


def test_root_command_uses_configured_processing_dir(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    processing_dir = books_dir / "Em-Andamento"
    state = _resume_state(tmp_path)
    ResumeStateStore(processing_dir).save(state)
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"processing": "Em-Andamento"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="n\n0\n")

    assert result.exit_code == 0
    assert "Translations in progress were found." in result.output
    assert "Processing translations" in result.output
    assert "book.epub" in result.output
    assert "Detected translation was not resumed." in result.output
    assert "Canceled." in result.output


def test_root_command_in_developer_mode_skips_guided_prompts(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, ["--mode", "developer", "--preview", str(epub_path)])

    assert "Generate a translation preview?" not in result.output
    assert "Não foi possível ler o EPUB informado." in result.output


def test_common_mode_hides_technical_detail_for_expected_error(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreflightError(
                "Não foi possível ler o EPUB informado.",
                "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
                detail="book.epub: invalid zip header",
            )
        ),
    )

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível e tente novamente." in result.output
    assert "Detalhe técnico:" not in result.output
    assert "invalid zip header" not in result.output
    assert "Traceback" not in result.output


def test_developer_mode_shows_technical_detail_for_expected_error(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreflightError(
                "Não foi possível ler o EPUB informado.",
                "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
                detail="book.epub: invalid zip header",
            )
        ),
    )

    result = runner.invoke(app, ["--mode", "developer", "translate", str(epub_path)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Detalhe técnico:" in result.output
    assert "invalid zip header" in result.output
    assert "Traceback" not in result.output


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


def _library_config(isolated_config, books_dir, reader_app=None):
    data = {"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}
    if reader_app:
        data["reader_app"] = reader_app
    isolated_config.write_text(json.dumps(data) + "\n", encoding="utf-8")


def test_translate_command_stops_when_preflight_fails(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")

    def fail_preflight(**_kwargs: object) -> object:
        raise PreflightError("Cache check failed: no write permission", "Choose a writable cache path.")

    def fail_translate(*_args: object, **_kwargs: object) -> TranslationReport:
        raise AssertionError("translation should not start when preflight fails")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)
    _fake_translation(monkeypatch, fail_translate)

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


def test_translate_command_uses_output_dir_for_single_epub(tmp_path, monkeypatch):
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

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_dir / "book-pt.epub"
    assert "Keep this output location?" not in result.output


def test_translate_command_delegates_v2_review_export_from_exact_project_result(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    review_path = tmp_path / "book-review.csv"
    processing_dir = tmp_path / "processing"
    epub_path.write_bytes(b"fake epub")
    project_id = "00000000-0000-4000-8000-000000000148"
    job_id = "00000000-0000-4000-8000-000000000149"
    machine_id = "00000000-0000-4000-8000-000000000150"
    review_job_id = "00000000-0000-4000-8000-000000000151"
    review_artifact_id = "00000000-0000-4000-8000-000000000152"
    now = datetime(2026, 11, 5, tzinfo=UTC)
    digest = "sha256:" + "a" * 64
    machine = Artifact(machine_id, ArtifactRequest(project_id, job_id, "translated_output", "reconstruct", 1,
                                                   digest, "application/epub+zip"),
                       ArtifactState.READY, 2, now, now, 10, digest)
    translation = TranslateProjectResult(
        project_id, job_id, JobState.VALIDATED, artifact=machine,
        engine=EngineResult(documents=(object(),), translations=(object(),)),
        artifact_destination=output_path, project_revision=1)
    report = TranslationReport(output_path=output_path, input_path=epub_path, output_written=True,
                               detected_language="en", target_language="pt")
    monkeypatch.setattr("ayvu.interfaces.cli.settings._processing_dir", lambda _config: processing_dir)
    monkeypatch.setattr("ayvu.composition.cli.build_project_request", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("ayvu.composition.cli.resolve_project", lambda *_args, **_kwargs: SimpleNamespace(
        project=SimpleNamespace(project_id=project_id, source_sha256="b" * 64)))
    monkeypatch.setattr("ayvu.interfaces.cli.run_support._translate_in_project", lambda *_args, **_kwargs:
                        _BookOutcome(report=report, validation=OutputValidation(True, (), 1), job_result=translation))

    package_bytes = b"AYVU-REVIEW-PACKAGE,2,{}\r\n"
    package_fingerprint = "sha256:" + hashlib.sha256(package_bytes).hexdigest()
    identity = ReviewPackageIdentity(2, "rpk:v1:" + "c" * 64, package_fingerprint, project_id, 1,
                                     "b" * 64, 10, machine_id, 2, digest, 1, 1)
    review_artifact = Artifact(
        review_artifact_id, ArtifactRequest(project_id, review_job_id, "review_package", "review_export", 1,
                                           digest, "text/csv"), ArtifactState.READY, 2, now, now,
        len(package_bytes), package_fingerprint)
    captured = {}

    class FakeExport:
        def execute(self, command):
            captured["command"] = command
            review_path.write_bytes(package_bytes)
            return ReviewExportResult(ReviewOutcome.READY, "review_package_exported", identity,
                                      review_artifact, review_job_id)

    class FakeUseCases:
        export = FakeExport()

        @staticmethod
        def cancellation():
            return SimpleNamespace(token=None)

    monkeypatch.setattr("ayvu.interfaces.cli.run.open_review_use_cases", lambda *_args, **_kwargs: FakeUseCases())
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 0
    assert "Review file saved to:" in result.output
    assert "Review file" in result.output
    assert review_path.read_bytes().startswith(b"AYVU-REVIEW-PACKAGE,2,")
    assert captured["command"].translation is translation
    assert captured["command"].destination == review_path
    assert captured["command"].overwrite is False


def test_translate_command_rejects_existing_review_output_without_overwrite(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    review_path = tmp_path / "book-review.csv"
    epub_path.write_bytes(b"fake epub")
    review_path.write_text("already here", encoding="utf-8")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run when review output already exists")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 1
    assert "Arquivo de revisão já existe." in result.output
    assert "Use --overwrite" in result.output
    assert str(review_path) not in result.output
    assert review_path.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_rejects_review_output_with_dry_run(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    review_path = tmp_path / "book-review.csv"
    epub_path.write_bytes(b"fake epub")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run for dry-run review output")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--dry-run",
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 1
    assert "Não é possível gerar arquivo de revisão em dry-run." in result.output
    assert not review_path.exists()
    assert "Traceback" not in result.output


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


def test_translate_command_cache_only_saves_missing_texts_file(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    missing_path = tmp_path / "missing.txt"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_from_cache=1,
        texts_missing=2,
        missing_texts=["Hello reader.", "Another line."],
        output_path=output_path,
        output_written=True,
        input_path=epub_path,
        detected_language="en",
        target_language="pt",
    )
    captured: dict[str, object] = {}

    def fake_translate_epub(*_args: object, **kwargs: object) -> TranslationReport:
        captured["options"] = kwargs["options"]
        return report

    def fake_preflight(**kwargs: object) -> SimpleNamespace:
        captured["preflight_kwargs"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate_epub)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, warnings=[], document_count=1),
    )

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache-only",
            "--missing-output",
            str(missing_path),
        ],
    )

    assert result.exit_code == 0
    assert "Texts missing" in result.output
    assert captured["options"].cache_only is True
    assert captured["preflight_kwargs"]["cache_only"] is True
    assert missing_path.exists()
    content = missing_path.read_text(encoding="utf-8")
    assert "Missing texts: 2" in content
    assert "Hello reader." in content
    assert "Another line." in content


def test_translate_command_cache_only_require_full_cache_blocks_output(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    missing_path = tmp_path / "missing.txt"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_from_cache=1,
        texts_missing=2,
        missing_texts=["Hello reader.", "Another line."],
        output_path=output_path,
        output_written=False,
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

    def fail_validate(*_args: object, **_kwargs: object) -> ValidationResult:
        raise AssertionError("validation must not run when output is blocked")

    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", fail_validate)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache-only",
            "--require-full-cache",
            "--missing-output",
            str(missing_path),
        ],
    )

    assert result.exit_code == 1
    assert "Cache incompleto" in result.output
    assert "(cache incompleto, nenhum arquivo gerado)" in result.output
    assert missing_path.exists()


class FakeCache:
    def __enter__(self) -> "FakeCache":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None


def _resume_state(tmp_path: Path) -> TranslationResumeState:
    return TranslationResumeState.create(
        input_path=tmp_path / "Original" / "book.epub",
        output_path=tmp_path / "Traduzidos" / "book-pt.epub",
        cache_path=tmp_path / "cache.sqlite",
        translator_name="libretranslate",
        url="http://localhost:5000",
        glossary_path=None,
        options=TranslationOptions(
            language_pair=LanguagePair(source="en", target="pt"),
            chunk_limit=1500,
        ),
        overwrite=False,
        timeout=30.0,
        retries=2,
    )


def _mock_translation_pipeline(monkeypatch, calls: dict[str, object]) -> None:
    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["options"] = kwargs["options"]
        return TranslationReport(
            output_path=output_path,
            input_path=input_path,
            detected_language=kwargs["options"].source,
            target_language=kwargs["options"].target,
        )

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)


def test_translate_auto_detects_source_language_from_epub(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(app, ["--mode", "developer", "translate", str(minimal_epub_path)])

    assert result.exit_code == 0
    assert calls["options"].source == "en"
    assert "Translation plan" in result.output
    assert "inferido do EPUB" in result.output


def test_translate_explicit_source_overrides_epub_metadata(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--source", "fr"],
    )

    assert result.exit_code == 0
    assert calls["options"].source == "fr"
    assert "inferido do EPUB" not in result.output


def test_translate_command_passes_execution_controls_to_preflight_and_resume(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    processing_dir = tmp_path / "Processando"
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--requests-per-second",
            "3.5",
            "--retry-backoff",
            "0.25",
            "--retry-backoff-max",
            "2",
            "--workers",
            "3",
        ],
    )

    preflight = calls["preflight"]
    state_paths = list(processing_dir.glob("*.ayvu-state.json"))
    saved_state = ResumeStateStore(processing_dir).load(state_paths[0])
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 3.5
    assert preflight["retry_backoff"] == 0.25
    assert preflight["retry_backoff_max"] == 2.0
    assert calls["options"].workers == 3
    assert saved_state.requests_per_second == 3.5
    assert saved_state.retry_backoff == 0.25
    assert saved_state.retry_backoff_max == 2.0
    assert saved_state.workers == 3
    assert "Workers" in result.output
    assert "3 (parallel" in result.output
    assert "3.5 requests/second" in result.output
    assert "shared across workers" in result.output
    assert "2 retries" in result.output
    assert "0.25s initial backoff" in result.output


def test_translate_command_uses_low_performance_profile(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "low",
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 1.0
    assert preflight["retries"] == 4
    assert preflight["retry_backoff"] == 1.0
    assert preflight["retry_backoff_max"] == 12.0
    assert options.workers == 1
    assert options.chunk_limit == 1500
    assert "Performance profile" in result.output
    assert "low" in result.output
    assert "1 requests/second" in result.output
    assert "4 retries" in result.output


def test_translate_command_explicit_execution_flags_override_performance_profile(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "high",
            "--workers",
            "2",
            "--requests-per-second",
            "3",
            "--retries",
            "5",
            "--retry-backoff",
            "0.75",
            "--retry-backoff-max",
            "6",
            "--chunk-limit",
            "2200",
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 3.0
    assert preflight["retries"] == 5
    assert preflight["retry_backoff"] == 0.75
    assert preflight["retry_backoff_max"] == 6.0
    assert options.workers == 2
    assert options.chunk_limit == 2200
    assert "Performance profile" in result.output
    assert "high" in result.output
    assert "2 (parallel" in result.output
    assert "3 requests/second shared across workers" in result.output


def test_translate_command_uses_profile_target_and_glossary(
    isolated_config,
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    profile_glossary_path = isolated_config.parent / "glossaries" / "technical.json"
    profile_glossary_path.parent.mkdir(parents=True)
    profile_glossary_path.write_text('{}')
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
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
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--profile", "technical"],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert "Profile" in result.output
    assert "technical" in result.output
    assert preflight["language_pair"].target == "es"
    assert preflight["glossary_path"] == isolated_config.parent / "glossaries" / "technical.json"
    assert options.target == "es"


def test_translate_command_explicit_target_and_glossary_override_profile(
    isolated_config,
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    profile_glossary_path = isolated_config.parent / "glossaries" / "technical.json"
    explicit_glossary_path = tmp_path / "custom.json"
    explicit_glossary_path.write_text('{}')
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "technical": {
                        "target_language": "es",
                        "glossary": str(profile_glossary_path),
                        "style": "technical",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--profile",
            "technical",
            "--target",
            "fr",
            "--glossary",
            str(explicit_glossary_path),
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["language_pair"].target == "fr"
    assert preflight["glossary_path"] == explicit_glossary_path
    assert options.target == "fr"


def test_translate_metadata_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--translate-metadata"],
    )

    assert result.exit_code == 0
    assert calls["options"].translate_metadata


def test_translate_alt_text_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--translate-alt-text"],
    )

    assert result.exit_code == 0
    assert calls["options"].translate_alt_text


def test_translate_chapters_option_reaches_translation_options_and_prints_selection(
    minimal_epub_path, tmp_path, monkeypatch
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "2"],
    )

    options = calls["options"]
    assert result.exit_code == 0
    assert options.chapter_selection is not None
    assert options.chapter_selection.source == "2"
    assert "Selected chapters" in result.output
    assert "Chapter Two" in result.output
    assert "text/chapter2.xhtml" in result.output


def test_translate_warns_when_epub_language_metadata_is_missing(tmp_path, monkeypatch):
    epub_path = tmp_path / "no-lang.epub"
    book = epub.EpubBook()
    book.set_identifier("ayvu-no-lang")
    book.set_title("No Language")
    book.add_author("Ayvu Tests")
    chapter = epub.EpubHtml(title="Ch", file_name="text/ch.xhtml")
    chapter.content = "<h1>Title</h1><p>Body.</p>"
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    epub.write_epub(str(epub_path), book)

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(app, ["--mode", "developer", "translate", str(epub_path)])

    assert result.exit_code == 0
    assert "Idioma do EPUB ausente ou inválido" in result.output
    assert calls["options"].source == "en"


def test_translate_common_mode_shows_detected_source_hint(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(minimal_epub_path)],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Translation plan" in result.output
    assert "Idioma de origem detectado do EPUB: en" in result.output


def _write_review_csv_file(path: Path, target: str = "pt") -> Path:
    segment = ReviewSegment(
        segment_id="c0001-s0001",
        source_epub="book.epub",
        output_epub="book-pt.epub",
        chapter_index=1,
        chapter_name="text/chapter.xhtml",
        document_name="text/chapter.xhtml",
        document_path="OEBPS/text/chapter.xhtml",
        segment_kind="text",
        source_language="en",
        target_language=target,
        original="Hello reader.",
        translated="Ola leitor.",
    )
    return write_review_csv(path, [segment])


def test_cli_console_width_does_not_depend_on_the_environment():
    from ayvu.interfaces.cli.render import console

    assert console.width == 500


def _fake_http(monkeypatch, *, get=(), post=()):
    import requests as requests_module

    class Session:
        def mount(self, prefix, adapter):
            assert prefix == "http://"

        def __init__(self):
            self.calls = []
            self.get_script, self.post_script = list(get), list(post)

        def _next(self, script):
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def get(self, url, *, timeout, allow_redirects=False):
            assert allow_redirects is False
            self.calls.append(("GET", url))
            return self._next(self.get_script)

        def post(self, url, *, json, timeout, allow_redirects=False):
            assert allow_redirects is False
            self.calls.append(("POST", url, json))
            return self._next(self.post_script)

    session = Session()
    monkeypatch.setattr(requests_module, "Session", lambda: session)
    return session


def _http(status, body):
    import requests as requests_module

    response = requests_module.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.url = "http://localhost:5000/x"
    return response


def test_common_mode_failure_shows_no_technical_detail(monkeypatch):
    _fake_http(monkeypatch, post=[_http(500, "oops")])
    result = runner.invoke(app, ["--mode", "common", "test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "Detalhe técnico" not in result.output and "provider_server_error" not in result.output


@pytest.mark.parametrize("command", ["languages", "test-translator"])
def test_bad_settings_report_a_code_without_a_traceback(command):
    result = runner.invoke(app, [command, "--requests-per-second", "0"])
    assert result.exit_code == 1 and "invalid_provider_settings" in result.output and "Traceback" not in result.output


def test_credentials_in_the_url_never_reach_the_output(monkeypatch):
    import requests as requests_module

    url = "http://user:hunter2@private.example.test:5000"
    _fake_http(monkeypatch, get=[requests_module.exceptions.ConnectionError(f"cannot reach {url}")], post=[requests_module.exceptions.ConnectionError(f"cannot reach {url}")])
    for command in ("languages", "test-translator"):
        result = runner.invoke(app, [command, "--url", url, "--retries", "0"])
        assert result.exit_code == 1 and "hunter2" not in result.output and "private.example.test" not in result.output
