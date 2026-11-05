import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED

import pytest
from ebooklib import epub
from epub_fakes import epub_bytes
from typer.testing import CliRunner

from ayvu.cache import CacheKey, TranslationCache
import typer

import ayvu.cli as cli_module
from ayvu.artifact import Artifact, ArtifactRequest, ArtifactState
from ayvu.cli import (
    DEFAULT_PREVIEW_DOCUMENT_LIMIT,
    _BookOutcome,
    _build_translation_memory_options,
    _offer_markdown_report,
    _render_markdown_report,
    _save_markdown_report,
    app,
)
from ayvu.domain import (
    LanguagePair,
    OutputPlan,
    TranslationMemoryOptions,
    TranslationOptions,
    UserMode,
)
from ayvu.epub_io import TranslationReport
from ayvu.job import JobState
from ayvu.job_events import JobFailure, JobFailureFamily
from ayvu.glossary import GlossaryUsage
from ayvu.markdown_writer import MarkdownDirectoryWriter
from ayvu.html_translate import HtmlTranslationStats
from ayvu.preflight import PreflightError
from ayvu.resume import COMPLETED_STATUS, ResumeStateStore, TranslationResumeState
from ayvu.review_application import ReviewExportResult, ReviewOutcome, ReviewPackageIdentity
from ayvu.review_export import ReviewSegment, write_review_csv
from ayvu.translation_engine import EngineResult
from ayvu.translate_project import OutputValidation, TranslateProjectResult
from ayvu.translator import TranslatorError, TranslatorLanguage
from ayvu.validation import ValidationResult


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
        validation = cli_module.validate_output(book.output_plan.path, on_progress=hooks.on_validation) if report.output_written else None
        return _BookOutcome(report=report, validation=validation)

    monkeypatch.setattr("ayvu.cli._translate_in_project", run)
    monkeypatch.setattr("ayvu.cli.translate_without_project", fake_translate)


def test_translate_resolves_and_reuses_project_before_preflight(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'project-processing'
    monkeypatch.setattr('ayvu.cli._processing_dir', lambda _config: processing)
    calls = []
    def stop(**kwargs):
        calls.append(kwargs)
        raise PreflightError('synthetic-stop', 'test complete')
    monkeypatch.setattr('ayvu.cli.run_translation_preflight', stop)
    original = minimal_epub_path.read_bytes()
    args = ['translate', str(minimal_epub_path), '--source', 'en', '--target', 'pt',
            '--output', str(tmp_path / 'translated.epub')]
    for _ in range(2):
        result = runner.invoke(app, args)
        assert result.exit_code == 1 and 'synthetic-stop' in result.output
    manifests = list(processing.glob('projects/*/project.json'))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())['revision'] == 1
    assert len(calls) == 2
    assert minimal_epub_path.read_bytes() == original


def test_project_storage_failure_prevents_preflight(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'blocked-processing'
    processing.write_bytes(b'blocked')
    monkeypatch.setattr('ayvu.cli._processing_dir', lambda _config: processing)
    calls = []
    monkeypatch.setattr('ayvu.cli.run_translation_preflight', lambda **kw: calls.append(kw))
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en',
                                 '--output', str(tmp_path / 'translated.epub')])
    assert result.exit_code == 1
    assert calls == []
    assert 'projeto local' in result.output
    assert not (tmp_path / 'translated.epub').exists()


def test_project_rejects_secret_endpoint_without_reflecting_it(minimal_epub_path, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('ayvu.cli.run_translation_preflight', lambda **kw: calls.append(kw))
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en',
                                 '--url', 'http://user:secret-canary@localhost',
                                 '--output', str(tmp_path / 'translated.epub')])
    assert result.exit_code == 1
    assert calls == []
    assert 'secret-canary' not in result.output
    assert 'sem usuário' in result.output


def test_dry_run_does_not_require_project_storage(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'blocked'
    processing.write_bytes(b'blocked')
    monkeypatch.setattr('ayvu.cli._processing_dir', lambda _config: processing)
    calls = []
    def stop(**kwargs):
        calls.append(kwargs)
        raise PreflightError('synthetic-dry-run-stop', 'test complete')
    monkeypatch.setattr('ayvu.cli.run_translation_preflight', stop)
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en', '--dry-run'])
    assert len(calls) == 1
    assert 'synthetic-dry-run-stop' in result.output
    assert processing.read_bytes() == b'blocked'


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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "Processando")
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


def test_translate_command_has_clear_error_for_unknown_translator(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"not a real epub")

    result = runner.invoke(app, ["translate", str(epub_path), "--translator", "unknown", "--dry-run"])

    assert result.exit_code == 1
    assert "Não foi possível preparar o tradutor." in result.output
    assert "Detalhe técnico:" in result.output
    assert "Unsupported translator:" in result.output
    assert "unknown" in result.output
    assert "Use --translator libretranslate." in result.output
    assert "Traceback" not in result.output


def test_languages_command_lists_translator_languages(monkeypatch):
    calls: dict[str, object] = {}

    class FakeLanguageTranslator:
        def __init__(
            self,
            url: str,
            timeout: float,
            retries: int,
            requests_per_second: float | None = None,
            retry_backoff: float = 0.5,
            retry_backoff_max: float = 8.0,
        ) -> None:
            calls["url"] = url
            calls["timeout"] = timeout
            calls["retries"] = retries
            calls["requests_per_second"] = requests_per_second
            calls["retry_backoff"] = retry_backoff
            calls["retry_backoff_max"] = retry_backoff_max

        def list_languages(self) -> tuple[TranslatorLanguage, ...]:
            return (
                TranslatorLanguage(code="pt", name="Portuguese", targets=("en", "es")),
                TranslatorLanguage(code="en", name="English", targets=("pt",)),
            )

    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FakeLanguageTranslator)

    result = runner.invoke(
        app,
        [
            "languages",
            "--url",
            "http://localhost:5000",
            "--timeout",
            "2",
            "--retries",
            "0",
            "--requests-per-second",
            "3.5",
            "--retry-backoff",
            "0.25",
            "--retry-backoff-max",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "url": "http://localhost:5000",
        "timeout": 2.0,
        "retries": 0,
        "requests_per_second": 3.5,
        "retry_backoff": 0.25,
        "retry_backoff_max": 2.0,
    }
    assert "LibreTranslate languages" in result.output
    assert "Portuguese" in result.output
    assert "pt" in result.output
    assert "installed" in result.output
    assert "en, es" in result.output


def test_languages_command_reports_failure_without_traceback(monkeypatch):
    class FailingLanguageTranslator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def list_languages(self) -> tuple[TranslatorLanguage, ...]:
            raise TranslatorError("server unavailable")

    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FailingLanguageTranslator)

    result = runner.invoke(app, ["languages"])

    assert result.exit_code == 1
    assert "Não foi possível listar os idiomas." in result.output
    assert "server unavailable" not in result.output
    assert "provider_request_failed" in result.output
    assert "Inicie o LibreTranslate" in result.output
    assert "Traceback" not in result.output


def test_cache_inspect_command_lists_entries_by_language_pair(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    _seed_cache_entry(cache_path, "Hello", "Olá", created_at="2024-01-01 10:00:00")
    _seed_cache_entry(cache_path, "World", "Mundo", created_at="2024-01-02 10:00:00")
    _seed_cache_entry(cache_path, "Hola", "Olá", source="es", target="pt", created_at="2024-01-03 10:00:00")

    result = runner.invoke(app, ["cache", "inspect", "--cache", str(cache_path)])

    assert result.exit_code == 0
    assert "Cache summary" in result.output
    assert "en" in result.output
    assert "es" in result.output
    assert "pt" in result.output
    assert "2024-01-01 10:00:00" in result.output
    assert "3 cache entries" in result.output


def test_cache_clean_requires_filter_or_all(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    _seed_cache_entry(cache_path, "Hello", "Olá")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--yes"])

    assert result.exit_code == 1
    assert "escopo explícito" in result.output
    assert "Traceback" not in result.output


def test_cache_clean_dry_run_does_not_delete_entries(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    key = _seed_cache_entry(cache_path, "Hello", "Olá")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--source", "en", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "1 cache entry would be removed" in result.output
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == "Olá"


def test_cache_clean_deletes_confirmed_entries(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    removed_key = _seed_cache_entry(cache_path, "Hello", "Olá", target="pt")
    kept_key = _seed_cache_entry(cache_path, "Hello", "Hola", target="es")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--target", "pt", "--yes"])

    assert result.exit_code == 0
    assert "Deleted 1 cache entry" in result.output
    with TranslationCache(cache_path) as cache:
        assert cache.get(removed_key) is None
        assert cache.get(kept_key) == "Hola"


def test_cache_export_and_import_commands(tmp_path):
    source_cache = tmp_path / "source.sqlite"
    target_cache = tmp_path / "target.sqlite"
    export_path = tmp_path / "cache-export.json"
    key = _seed_cache_entry(source_cache, "Hello", "Olá")

    export_result = runner.invoke(
        app,
        ["cache", "export", str(export_path), "--cache", str(source_cache), "--source", "en"],
    )
    import_result = runner.invoke(
        app,
        ["cache", "import", str(export_path), "--cache", str(target_cache)],
    )

    assert export_result.exit_code == 0
    assert "Cache export saved to" in export_result.output
    assert "Entries exported: 1" in export_result.output
    assert import_result.exit_code == 0
    assert "Cache import" in import_result.output
    assert "Imported" in import_result.output
    with TranslationCache(target_cache) as cache:
        assert cache.get(key) == "Olá"


def test_cache_command_reports_invalid_before_without_traceback(tmp_path):
    cache_path = tmp_path / "translations.sqlite"

    result = runner.invoke(app, ["cache", "inspect", "--cache", str(cache_path), "--before", "not-a-date"])

    assert result.exit_code == 1
    assert "Data de cache inválida." in result.output
    assert "Traceback" not in result.output


def test_root_command_shows_processing_translation_state(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    state = _resume_state(tmp_path)
    ResumeStateStore(processing_dir).save(state)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)

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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, ["--mode", "developer", "--preview", str(epub_path)])

    assert "Generate a translation preview?" not in result.output
    assert "Não foi possível ler o EPUB informado." in result.output


def test_translate_command_in_developer_mode_skips_confirmations(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake")
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")

    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(PreflightError("failed", "next")),
    )

    result = runner.invoke(app, ["translate", str(epub_path)])

    assert result.exit_code == 1
    assert "Default output folder:" not in result.output
    assert "Keep this output location?" not in result.output
    assert "failed" in result.output
    assert "next" in result.output


def test_common_mode_hides_technical_detail_for_expected_error(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)

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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "Canceled." in result.output
    assert "Translations in progress were found." not in result.output
    assert "Invalid processing state files were found." not in result.output


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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)


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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

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
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, ["resume"])

    assert result.exit_code == 1
    assert "Several translations in progress match the request." in result.output
    assert "Specify the EPUB and --target" in result.output
    assert "Traceback" not in result.output


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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.cli.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr(
        "ayvu.provider_composition.LibreTranslateTranslator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("languages should not be listed")),
    )
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.cli.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FakeLanguageTranslator)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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


def test_root_command_creates_guided_glossary(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="5\n1\nTechnical Terms\nGame Loop\n1\nloop de jogo\ny\nObserver\n2\nn\ny\n",
    )

    glossary_path = isolated_config.parent / "glossaries" / "technical-terms.json"
    assert result.exit_code == 0
    assert "Glossaries" in result.output
    assert "Glossary preview" in result.output
    assert "Glossary saved:" in result.output
    assert json.loads(glossary_path.read_text(encoding="utf-8")) == {
        "Game Loop": {"rule": "translate", "translation": "loop de jogo"},
        "Observer": {"rule": "preserve"},
    }


def test_root_command_edits_guided_glossary(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    glossary_dir = isolated_config.parent / "glossaries"
    glossary_dir.mkdir()
    glossary_path = glossary_dir / "technical.json"
    glossary_path.write_text('{"Observer": {"rule": "preserve"}}\n', encoding="utf-8")

    result = runner.invoke(
        app,
        [],
        input="5\n2\n1\nObject Pool\n1\npool de objetos\nn\ny\n",
    )

    assert result.exit_code == 0
    assert "Edit saved glossary" in result.output
    assert "Saved glossaries" in result.output
    assert "Term added:" in result.output
    assert json.loads(glossary_path.read_text(encoding="utf-8")) == {
        "Observer": {"rule": "preserve"},
        "Object Pool": {"rule": "translate", "translation": "pool de objetos"},
    }


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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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


def _library_config(isolated_config, books_dir, reader_app=None):
    data = {"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}
    if reader_app:
        data["reader_app"] = reader_app
    isolated_config.write_text(json.dumps(data) + "\n", encoding="utf-8")


def test_a_folder_pointing_outside_the_library_is_told_and_not_taken_for_an_empty_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    books_dir.mkdir()
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "z.epub").write_bytes(b"x")
    (books_dir / "Original").symlink_to(outside)
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n")

    assert result.exit_code == 0
    assert "Library warning (1)" in result.output
    assert "points outside" in result.output


def test_skipped_files_are_told_while_the_valid_books_stay_listed(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    (original / "valid.epub").write_bytes(b"x")
    (original / "a\tb.epub").write_bytes(b"x")
    outside = tmp_path / "z.epub"
    outside.write_bytes(b"x")
    (original / "link.epub").symlink_to(outside)
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n0\n")

    assert result.exit_code == 0
    assert "valid" in result.output
    assert "Library warning (1):" in result.output and "not a file inside" in result.output
    assert "invalid characters" in result.output


def test_a_long_name_that_the_filesystem_accepts_stays_in_the_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    (original / ("L" * 240 + ".epub")).write_bytes(b"x")
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n0\n")

    assert result.exit_code == 0
    assert "Library has no EPUB books yet." not in result.output
    assert "Library warning" not in result.output


def test_book_information_keeps_the_full_paths(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    books_dir = tmp_path / "Biblioteca"
    (books_dir / "Original").mkdir(parents=True)
    (books_dir / "Traduzidos").mkdir()
    (books_dir / "Original" / "book.epub").write_bytes(b"x")
    (books_dir / "Traduzidos" / "book-pt.epub").write_bytes(b"x")
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n1\n3\n0\n")

    flat = "".join(result.output.split())
    assert result.exit_code == 0
    assert "".join(str(books_dir / "Original" / "book.epub").split()) in flat
    assert "".join(str(books_dir / "Traduzidos" / "book-pt.epub").split()) in flat


def test_a_book_replaced_after_the_listing_is_not_blamed_on_the_reader(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    book = original / "book.epub"
    book.write_bytes(b"x")
    _library_config(isolated_config, books_dir, reader_app="reader")
    calls = []
    monkeypatch.setattr("ayvu.library_adapters.subprocess.Popen", lambda argv: calls.append(argv))
    real_book_actions = cli_module._library_book_actions

    def replace_then_list(listed):
        book.write_bytes(b"replaced")
        return real_book_actions(listed)

    monkeypatch.setattr(cli_module, "_library_book_actions", replace_then_list)

    result = runner.invoke(app, [], input="3\n1\n1\n")

    assert calls == []
    assert "Could not open EPUB" in result.output
    assert "Open the library again" in result.output
    assert "Configure Reader app" not in result.output


def test_root_command_shows_empty_guided_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="3\n")

    assert result.exit_code == 0
    assert "Open library" in result.output
    assert "Library has no EPUB books yet." in result.output
    assert str(books_dir / "Original") in result.output.replace("\n", "")
    assert str(books_dir / "Traduzidos") in result.output.replace("\n", "")
    assert "Library warning" not in result.output


def test_root_command_opens_library_translation(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    translated_dir = books_dir / "Traduzidos"
    original_dir.mkdir(parents=True)
    translated_dir.mkdir()
    (original_dir / "Book.epub").write_bytes(b"")
    translated_path = translated_dir / "Book-pt.epub"
    translated_path.write_bytes(b"")
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "reader_app": "foliate",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    opened: list[list[str]] = []
    from ayvu.library_adapters import ReaderLauncher
    monkeypatch.setattr("ayvu.cli.ReaderLauncher", lambda reader_app: ReaderLauncher(reader_app, runner=opened.append))

    result = runner.invoke(app, [], input="3\n1\n2\n")

    assert result.exit_code == 0
    assert "Library" in result.output
    assert "Book" in result.output
    assert "Traduzido - Português" in result.output
    assert "Open Traduzido - Português" in result.output
    assert opened == [["foliate", str(translated_path)]]


def test_root_command_shows_library_book_information(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    translated_dir = books_dir / "Traduzidos"
    original_dir.mkdir(parents=True)
    translated_dir.mkdir()
    original_path = original_dir / "Book.epub"
    translated_path = translated_dir / "Book-pt.epub"
    original_path.write_bytes(b"")
    translated_path.write_bytes(b"")
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="3\n1\n3\n0\n")

    assert result.exit_code == 0
    assert "Book information" in result.output
    assert "Original EPUB" in result.output
    assert "Translations" in result.output
    assert "Traduzido - Português" in result.output
    assert "Translation files" in result.output


def test_root_command_reports_library_open_failure(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    original_dir.mkdir(parents=True)
    (original_dir / "Book.epub").write_bytes(b"")
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    from ayvu.library_adapters import ReaderLauncher
    monkeypatch.setattr("ayvu.cli.ReaderLauncher", lambda reader_app: ReaderLauncher(reader_app, which=lambda _: None))

    result = runner.invoke(app, [], input="3\n1\n1\n")

    assert result.exit_code == 0
    assert "Could not open EPUB:" in result.output
    assert "No EPUB reader app" in result.output
    assert "Configure Reader app in Settings" in result.output


def test_root_command_settings_keeps_language_when_not_changed(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="4\n1\npt\n0\n")

    assert result.exit_code == 0
    assert "Settings" in result.output
    assert "Default target language" in result.output
    assert "Change default language" in result.output
    assert "Settings unchanged." in result.output
    assert "Settings closed." in result.output


def test_root_command_settings_changes_and_persists_language(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(app, [], input="4\n1\nes\n0\n")

    assert result.exit_code == 0
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "es"


def test_settings_unknown_fields_warn_and_refuse_lossy_update(isolated_config, monkeypatch):
    payload = b'{"version":1,"private_extension":{"value":"secret-canary"}}'
    isolated_config.write_bytes(payload)
    monkeypatch.setattr('ayvu.provider_composition.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    result = runner.invoke(app, [], input='4\n1\nes\n0\n')
    assert result.exit_code == 0
    assert 'somente leitura' in result.output
    assert 'Settings saved.' not in result.output
    assert 'não foi persistida' in result.output
    assert 'secret-canary' not in result.output
    assert isolated_config.read_bytes() == payload


def test_settings_save_failure_keeps_effective_session_values(isolated_config, monkeypatch):
    original = isolated_config.read_bytes()
    monkeypatch.setattr('ayvu.provider_composition.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    def fail(*args):
        raise OSError('private-canary-path')
    monkeypatch.setattr('ayvu.preferences_store.os.replace', fail)
    result = runner.invoke(app, [], input='4\n1\nes\n0\n')
    assert result.exit_code == 0
    assert 'Settings saved.' not in result.output
    assert 'não foi persistida' in result.output
    assert 'es' in result.output.split('não foi persistida.')[-1]
    assert 'private-canary' not in result.output
    assert isolated_config.read_bytes() == original


def test_settings_multiple_saves_advance_the_expected_revision(isolated_config, monkeypatch):
    monkeypatch.setattr('ayvu.provider_composition.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    result = runner.invoke(app, [], input='4\n1\nes\n4\nfoliate\n0\n')
    assert result.exit_code == 0
    assert result.output.count('Settings saved.') == 2
    saved = json.loads(isolated_config.read_text())
    assert saved['default_target_language'] == 'es'
    assert saved['reader_app'] == 'foliate'


def test_first_use_failed_save_retains_session_without_claiming_success(isolated_config, tmp_path, monkeypatch, minimal_epub_path):
    isolated_config.unlink()
    original_dir = tmp_path / 'Books' / 'Original'
    original_dir.mkdir(parents=True)
    (original_dir / 'first-use.epub').write_bytes(minimal_epub_path.read_bytes())
    monkeypatch.setattr('ayvu.provider_composition.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    def fail(*args):
        raise PermissionError('private-canary')
    monkeypatch.setattr('ayvu.preferences_store.os.replace', fail)
    result = runner.invoke(app, [], input=f'es\n{tmp_path / "Books"}\ny\n3\n0\n')
    assert result.exit_code == 0
    assert 'não foi persistida' in result.output
    assert 'Idioma padrão salvo:' not in result.output
    assert 'first-use' in result.output
    assert not isolated_config.exists()
    assert 'private-canary' not in result.output


def test_root_command_settings_changes_books_dir(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input=f"4\n2\n{books_dir}\n0\n")

    assert result.exit_code == 0
    assert "Books folder" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["books_dir"] == str(books_dir)


def test_root_command_settings_changes_feature_folder_names(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="4\n3\nOriginais\nPT\nAmostras\nRelatorios-MD\nEm-Andamento\n0\n",
    )

    assert result.exit_code == 0
    assert "Change feature folder names" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["folders"] == {
        "original": "Originais",
        "translated": "PT",
        "preview": "Amostras",
        "reports": "Relatorios-MD",
        "processing": "Em-Andamento",
    }


def test_root_command_settings_rejects_folder_paths(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="4\n3\nOriginais\nLivros/PT\nAmostras\nRelatorios\nProcessando\n0\n",
    )

    assert result.exit_code == 0
    assert "Invalid folder name:" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "pt"
    assert "folders" not in saved


def test_root_command_settings_changes_reader_app(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="4\n4\nfoliate\n0\n")

    assert result.exit_code == 0
    assert "Reader app" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["reader_app"] == "foliate"


def test_root_command_first_use_asks_and_saves_default_language(isolated_config, tmp_path, monkeypatch):
    isolated_config.unlink()
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(app, [], input=f"es\n{books_dir}\ny\n0\n")

    assert result.exit_code == 0
    assert "Primeiro uso do modo comum." in result.output
    assert "Idioma padrão salvo:" in result.output
    assert "Pasta base salva:" in result.output
    assert "Manter estes nomes de pastas?" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "es"
    assert saved["books_dir"] == str(books_dir)
    assert saved["folders"] == {
        "original": "Original",
        "translated": "Traduzidos",
        "preview": "Preview",
        "reports": "Relatorios",
        "processing": "Processando",
    }


def test_root_command_first_use_can_change_feature_folder_names(isolated_config, tmp_path, monkeypatch):
    isolated_config.unlink()
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.provider_composition.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(
        app,
        [],
        input=f"es\n{books_dir}\nn\nOriginais\nPT\nAmostras\nRelatorios-MD\nEm-Andamento\n0\n",
    )

    assert result.exit_code == 0
    assert "Feature folders" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["folders"] == {
        "original": "Originais",
        "translated": "PT",
        "preview": "Amostras",
        "reports": "Relatorios-MD",
        "processing": "Em-Andamento",
    }


def test_root_command_does_not_reask_when_config_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Primeiro uso do modo comum." not in result.output
    assert "Manter estes nomes de pastas?" not in result.output
    assert "Choose an option" in result.output


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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.cli.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, [], input=f"2\n{epub_path}\n1\n")

    assert result.exit_code == 0
    assert "Default target language:" in result.output
    assert calls["target"] == "es"
    assert calls["options"].target == "es"


def test_root_command_ignores_invalid_config(isolated_config, tmp_path, monkeypatch):
    isolated_config.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Configuração inválida ignorada:" in result.output
    assert "Choose an option" in result.output


def test_root_command_can_show_help_from_guided_menu(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="6\n")

    assert result.exit_code == 0
    assert "Show command help" in result.output
    assert "Usage:" in result.output
    assert "translate" in result.output


def test_preview_option_generates_preview_with_default_settings(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    preview_dir = tmp_path / "Preview"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> object:
        calls["preflight"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr("ayvu.cli.default_preview_books_dir", lambda: preview_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))

    result = runner.invoke(app, ["--preview", str(epub_path)])

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert "Preview output folder:" in result.output
    assert str(preview_dir) in result.output
    assert "book-preview.epub" in result.output
    assert "Preview salvo em:" in result.output
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == preview_dir / "book-preview.epub"
    assert preflight["epub_path"] == epub_path
    assert preflight["cache_path"] == Path(".cache/traducoes.sqlite")
    assert preflight["translator_name"] == "libretranslate"
    assert preflight["url"] == "http://localhost:5000"
    assert options.source == "en"
    assert options.target == "pt"
    assert options.max_documents == DEFAULT_PREVIEW_DOCUMENT_LIMIT


def test_preview_option_uses_configured_preview_dir(isolated_config, tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    books_dir = tmp_path / "Biblioteca"
    epub_path.write_bytes(b"fake epub")
    calls: dict[str, object] = {}
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"preview": "Amostras"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_translate(input_path: Path, output_path: Path, **kwargs: object) -> TranslationReport:
        calls["input_path"] = input_path
        calls["output_path"] = output_path
        calls["options"] = kwargs["options"]
        return TranslationReport(output_path=output_path, input_path=input_path, target_language="pt")

    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )

    result = runner.invoke(app, ["--preview", str(epub_path)])

    output_path = books_dir / "Amostras" / "book-preview.epub"
    assert result.exit_code == 0
    assert str(output_path.parent) in result.output.replace("\n", "")
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_path
    assert calls["options"].max_documents == DEFAULT_PREVIEW_DOCUMENT_LIMIT


def test_translate_command_stops_when_preflight_fails(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")

    def fail_preflight(**_kwargs: object) -> object:
        raise PreflightError("Cache check failed: no write permission", "Choose a writable cache path.")

    def fail_translate(*_args: object, **_kwargs: object) -> TranslationReport:
        raise AssertionError("translation should not start when preflight fails")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)
    _fake_translation(monkeypatch, fail_translate)

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    assert result.exit_code == 1
    assert "Default output folder:" in result.output
    assert "Keep this output location?" in result.output
    assert "Cache check failed: no write permission" in result.output
    assert "Choose a writable cache path." in result.output
    assert "Traceback" not in result.output


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

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: output_dir)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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


def test_translate_command_batch_writes_outputs_and_markdown_reports(tmp_path, monkeypatch):
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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli._reports_dir", lambda _config: reports_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )

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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(epub_path), "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0
    assert calls["input_path"] == epub_path
    assert calls["output_path"] == output_dir / "book-pt.epub"
    assert "Keep this output location?" not in result.output


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


def test_translate_command_batch_rejects_existing_output_without_overwrite(tmp_path, monkeypatch):
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

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

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
    assert "Batch translation summary" in result.output
    assert existing_output.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_batch_continues_after_failure_when_requested(tmp_path, monkeypatch):
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

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
    monkeypatch.setattr("ayvu.cli._reports_dir", lambda _config: reports_dir)
    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )

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
    monkeypatch.setattr("ayvu.cli._processing_dir", lambda _config: processing_dir)
    monkeypatch.setattr("ayvu.cli.build_project_request", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("ayvu.cli.resolve_project", lambda *_args, **_kwargs: SimpleNamespace(
        project=SimpleNamespace(project_id=project_id, source_sha256="b" * 64)))
    monkeypatch.setattr("ayvu.cli._translate_in_project", lambda *_args, **_kwargs:
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

    monkeypatch.setattr("ayvu.cli.open_review_use_cases", lambda *_args, **_kwargs: FakeUseCases())
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

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


def test_review_output_conflicts_redact_destination_path(tmp_path, capsys):
    review_path = tmp_path / "PRIVATE_REVIEW_PATH_CANARY_148.csv"
    review_path.write_text("exists", encoding="utf-8")
    with pytest.raises(typer.Exit):
        cli_module._resolve_review_output_path(review_path, False, False, UserMode.DEVELOPER)
    assert "PRIVATE_REVIEW_PATH_CANARY_148" not in capsys.readouterr().out


def test_translate_command_rejects_review_output_with_dry_run(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    review_path = tmp_path / "book-review.csv"
    epub_path.write_bytes(b"fake epub")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run for dry-run review output")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

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


def test_build_translation_memory_options_disabled_returns_none():
    assert (
        _build_translation_memory_options(
            enabled=False,
            apply_threshold=0.95,
            suggest_threshold=0.80,
            mode=UserMode.DEVELOPER,
        )
        is None
    )


def test_build_translation_memory_options_returns_validated_options():
    options = _build_translation_memory_options(
        enabled=True,
        apply_threshold=0.9,
        suggest_threshold=0.7,
        mode=UserMode.DEVELOPER,
    )

    assert options == TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7)


def test_build_translation_memory_options_rejects_invalid_thresholds():
    with pytest.raises(typer.Exit):
        _build_translation_memory_options(
            enabled=True,
            apply_threshold=0.5,
            suggest_threshold=0.9,
            mode=UserMode.DEVELOPER,
        )


def test_render_markdown_report_includes_memory_rows():
    report = TranslationReport(
        chapters_processed=1,
        texts_translated=2,
        texts_from_cache=1,
        texts_from_memory=3,
        memory_suggestions=4,
        output_path=Path("books/book-pt.epub"),
        input_path=Path("books/book.epub"),
        detected_language="en",
        target_language="pt",
    )

    markdown = _render_markdown_report(report, dry_run=False)

    assert "- Texts from memory: 3" in markdown
    assert "- Memory suggestions: 4" in markdown


def test_render_markdown_report_includes_translation_context():
    glossary_usage = GlossaryUsage(
        applied_terms={"Game Loop": 2, "Observer": 1},
        required_terms_missing=["Object Pool"],
        forbidden_terms_found={"AntiPattern": 1},
    )
    report = TranslationReport(
        chapters_processed=2,
        texts_translated=3,
        texts_from_cache=1,
        errors=["chapter.xhtml: failed\nwhile translating"],
        glossary_terms_configured=4,
        glossary_usage=glossary_usage,
        output_path=Path("books/book-pt.epub"),
        input_path=Path("books/book.epub"),
        detected_language="en",
        target_language="pt",
    )

    markdown = _render_markdown_report(report, dry_run=False)

    assert "# Translation report" in markdown
    assert "- Original EPUB: books/book.epub" in markdown
    assert "- Detected language: en" in markdown
    assert "- Translated language: pt" in markdown
    assert "- Output: books/book-pt.epub" in markdown
    assert "- Chapters processed: 2" in markdown
    assert "- Texts translated: 3" in markdown
    assert "- Texts from cache: 1" in markdown
    assert "- Errors: 1" in markdown
    assert "- Glossary terms configured: 4" in markdown
    assert "- Glossary terms applied: 3" in markdown
    assert "- Required glossary terms missing: 1" in markdown
    assert "- Forbidden glossary terms found: 1" in markdown
    assert "## Glossary usage" in markdown
    assert "- Game Loop: 2" in markdown
    assert "- Observer: 1" in markdown
    assert "## Glossary warnings" in markdown
    assert "- Required terms missing: Object Pool" in markdown
    assert "- Forbidden terms found: AntiPattern (1)" in markdown
    assert "- chapter.xhtml: failed while translating" in markdown


def test_save_markdown_report_uses_default_reports_dir_without_overwriting(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    report = TranslationReport(
        output_path=Path("book-pt.epub"),
        input_path=Path("book.epub"),
        target_language="pt",
    )
    monkeypatch.setattr("ayvu.cli._default_reports_dir", lambda: reports_dir)

    first_path = _save_markdown_report(report, dry_run=False)
    second_path = _save_markdown_report(report, dry_run=False)

    assert first_path == reports_dir / "book-pt-report.md"
    assert second_path == reports_dir / "book-pt-report-2.md"
    assert first_path.read_text(encoding="utf-8").startswith("# Translation report")
    assert second_path.read_text(encoding="utf-8").startswith("# Translation report")


def test_save_markdown_report_uses_configured_reports_dir(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"reports": "Relatorios-MD"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = TranslationReport(
        output_path=Path("book-pt.epub"),
        input_path=Path("book.epub"),
        target_language="pt",
    )

    report_path = _save_markdown_report(report, dry_run=False)

    assert report_path == books_dir / "Relatorios-MD" / "book-pt-report.md"
    assert report_path.read_text(encoding="utf-8").startswith("# Translation report")


def test_offer_markdown_report_does_not_save_when_declined(monkeypatch):
    saved = False

    def fake_save_report(_report: TranslationReport, _dry_run: bool) -> Path:
        nonlocal saved
        saved = True
        return Path("report.md")

    monkeypatch.setattr("ayvu.cli.typer.confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("ayvu.cli._save_markdown_report", fake_save_report)

    _offer_markdown_report(TranslationReport(), dry_run=False, mode=UserMode.COMMON)

    assert not saved


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
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)
    monkeypatch.setattr("ayvu.cli.validate_output", lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1))
    monkeypatch.setattr("ayvu.cli._default_reports_dir", lambda: reports_dir)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)

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
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=False, warnings=[warning], document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._default_reports_dir", lambda: reports_dir)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)

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


def test_translate_command_handles_keyboard_interrupt_cleanly(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    cache_path = tmp_path / "cache.sqlite"
    epub_path.write_bytes(b"fake epub")

    def interrupt_translation(*_args: object, **kwargs: object) -> TranslationReport:
        kwargs["on_chapter_start"](1, 3, "chapter-one.xhtml")
        kwargs["on_text_processed"]("translated")
        kwargs["on_text_processed"]("cache")
        kwargs["on_text_processed"]("error")
        kwargs["on_chapter_done"](1, 3, "chapter-one.xhtml", HtmlTranslationStats())
        kwargs["on_chapter_start"](2, 3, "chapter-two.xhtml")
        raise KeyboardInterrupt

    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "Processando")
    monkeypatch.setattr(
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, interrupt_translation)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache",
            str(cache_path),
            "--workers",
            "2",
            "--requests-per-second",
            "2.5",
            "--retries",
            "4",
            "--retry-backoff",
            "1",
            "--retry-backoff-max",
            "10",
        ],
    )

    assert result.exit_code == 1
    assert "Translation interrupted by user." in result.output
    assert "Partial translation progress" in result.output
    assert "Chapters processed" in result.output
    assert "1/3" in result.output
    assert "Texts processed" in result.output
    assert "3" in result.output
    assert "Texts translated" in result.output
    assert "Texts from cache" in result.output
    assert "Text errors" in result.output
    assert "Workers" in result.output
    assert "2 (parallel" in result.output
    assert "2.5 requests/second" in result.output
    assert "shared across workers" in result.output
    assert "4 retries" in result.output
    assert "chapter-two.xhtml" in result.output
    assert "Cached translations saved before the interruption can be reused" in result.output
    assert str(cache_path) in result.output
    assert "Translated EPUB was not written:" in result.output
    assert "Traceback" not in result.output
    assert not output_path.exists()


def test_translate_command_require_full_cache_requires_cache_only(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")

    result = runner.invoke(app, ["translate", str(epub_path), "--require-full-cache"])

    assert result.exit_code == 1
    assert "--require-full-cache exige --cache-only" in result.output


def test_translate_command_missing_output_requires_cache_only(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")

    result = runner.invoke(
        app, ["translate", str(epub_path), "--missing-output", str(tmp_path / "missing.txt")]
    )

    assert result.exit_code == 1
    assert "--missing-output exige --cache-only" in result.output


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

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate_epub)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
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
        "ayvu.cli.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)

    def fail_validate(*_args: object, **_kwargs: object) -> ValidationResult:
        raise AssertionError("validation must not run when output is blocked")

    monkeypatch.setattr("ayvu.cli.validate_output", fail_validate)

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


def test_inspect_command_reports_invalid_epub_without_traceback(tmp_path):
    epub_path = tmp_path / "bad.epub"
    epub_path.write_bytes(b"not a real epub")

    result = runner.invoke(app, ["inspect", str(epub_path)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível" in result.output
    assert "Detalhe técnico: corrupt_archive" in result.output
    assert "Traceback" not in result.output


def test_extract_says_the_output_is_the_problem_when_the_book_is_fine(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(epub_bytes())
    output = tmp_path / "taken"
    output.write_text("mine")

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output)])

    assert result.exit_code == 1
    assert "Não foi possível gravar na pasta de saída." in result.output
    assert "EPUB válido" not in result.output
    assert "Traceback" not in result.output
    assert output.read_text() == "mine"


def test_inspect_says_a_protected_book_is_protected_and_not_broken(tmp_path):
    epub_path = tmp_path / "book.epub"
    encryption = (b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                  b'xmlns:e="http://www.w3.org/2001/04/xmlenc#"><e:EncryptedData><e:EncryptionMethod '
                  b'Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/><e:CipherData><e:CipherReference '
                  b'URI="OEBPS/text/one.xhtml"/></e:CipherData></e:EncryptedData></encryption>')
    epub_path.write_bytes(epub_bytes(add=(("META-INF/encryption.xml", encryption, ZIP_DEFLATED),)))

    result = runner.invoke(app, ["--mode", "common", "inspect", str(epub_path)])

    assert result.exit_code == 1
    assert "EPUB válido" not in result.output
    assert "proteção" in result.output


def test_extract_blames_the_chapter_name_when_it_cannot_become_a_file(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(epub_bytes())
    monkeypatch.setattr(MarkdownDirectoryWriter, "_name", staticmethod(
        lambda _ordinal, _stem: (None, JobFailure(JobFailureFamily.POLICY, "reserved_output_name"))))
    output = tmp_path / "out"

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output)])

    assert result.exit_code == 1
    assert "nome que não pode virar arquivo" in result.output
    assert "EPUB válido" not in result.output
    assert not list(output.glob("*.md"))


def test_extract_command_reports_invalid_epub_without_traceback(tmp_path):
    epub_path = tmp_path / "bad.epub"
    epub_path.write_bytes(b"not a real epub")
    output_dir = tmp_path / "out"

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output_dir)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível" in result.output
    assert "Detalhe técnico: corrupt_archive" in result.output
    assert "Traceback" not in result.output


def test_inspect_command_preserves_the_six_current_fields(minimal_epub_path, tmp_path):
    result = runner.invoke(app, ["inspect", str(minimal_epub_path)])
    assert result.exit_code == 0
    for field in ("Path", "Title", "Authors", "Language", "Documents", "Items"):
        assert field in result.output
    assert str(minimal_epub_path) in result.output
    assert "Minimal Test Book" in result.output
    assert "Ayvu Tests" in result.output
    assert re.search(r"Language\s+│\s*en", result.output)
    assert re.search(r"Documents\s+│\s*3", result.output)
    assert re.search(r"Items\s+│\s*5", result.output)


def test_extract_command_preserves_filenames_text_order_and_original(minimal_epub_path, tmp_path):
    original = hashlib.sha256(minimal_epub_path.read_bytes()).hexdigest()
    output_dir = tmp_path / "extracted"
    result = runner.invoke(app, ["extract", str(minimal_epub_path), "--output", str(output_dir)])
    assert result.exit_code == 0
    assert f"Extracted 3 Markdown files to {output_dir}" in result.output
    assert [path.name for path in sorted(output_dir.glob("*.md"))] == [
        "001-chapter1.md", "002-chapter2.md", "003-nav.md",
    ]
    assert (output_dir / "001-chapter1.md").read_text(encoding="utf-8") == (
        "Chapter One\n\nHello reader. Visit\n\nchapter two\n\n.\n"
    )
    assert (output_dir / "002-chapter2.md").read_text(encoding="utf-8") == "Chapter Two\n\nGoodbye reader.\n"
    assert (output_dir / "003-nav.md").read_text(encoding="utf-8") == "Minimal Test Book\n\nChapter One\n\nChapter Two\n"
    assert hashlib.sha256(minimal_epub_path.read_bytes()).hexdigest() == original


def test_extract_command_keeps_the_nonempty_directory_conflict_message(tmp_path):
    source = tmp_path / "source.epub"
    source.write_bytes(b"not an EPUB")
    output = tmp_path / "out"
    output.mkdir()
    unrelated = output / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    result = runner.invoke(app, ["extract", str(source), "--output", str(output)])
    assert result.exit_code == 1
    assert f"Output directory is not empty: {output}" in result.output
    assert "Use --overwrite to write into it." in result.output
    assert unrelated.read_text(encoding="utf-8") == "keep"


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

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)


def test_translate_auto_detects_source_language_from_epub(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(app, ["--mode", "developer", "translate", str(minimal_epub_path)])

    assert result.exit_code == 0
    assert calls["options"].source == "en"
    assert "Translation plan" in result.output
    assert "inferido do EPUB" in result.output


def test_translate_explicit_source_overrides_epub_metadata(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: processing_dir)
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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


def test_translate_command_reports_unknown_performance_profile_without_traceback(
    minimal_epub_path,
    monkeypatch,
):
    called = False

    def fail_preflight(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("preflight should not run for an unknown performance profile")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "turbo",
        ],
    )

    assert result.exit_code == 1
    assert "Perfil de performance não encontrado." in result.output
    assert "--performance-profile" in result.output
    assert "Traceback" not in result.output
    assert not called


def test_translate_command_rejects_invalid_workers(minimal_epub_path, monkeypatch):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with invalid workers")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--workers", "0"],
    )

    assert result.exit_code == 1
    assert "Quantidade de workers inválida." in result.output
    assert "Use --workers com valor 1 ou maior." in result.output
    assert "Traceback" not in result.output


def test_translate_command_rejects_parallel_workers_with_translation_memory(
    minimal_epub_path,
    monkeypatch,
):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with incompatible worker options")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--workers",
            "2",
            "--translation-memory",
        ],
    )

    assert result.exit_code == 1
    assert "--workers é incompatível com --translation-memory nesta versão." in result.output
    assert "Traceback" not in result.output


def test_translate_command_rejects_high_performance_profile_with_translation_memory(
    minimal_epub_path,
    monkeypatch,
):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with incompatible performance profile")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "high",
            "--translation-memory",
        ],
    )

    assert result.exit_code == 1
    assert "--workers é incompatível com --translation-memory nesta versão." in result.output
    assert "Traceback" not in result.output


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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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


def test_translate_command_reports_unknown_profile_without_traceback(minimal_epub_path, monkeypatch):
    called = False

    def fail_preflight(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("preflight should not run for an unknown profile")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--profile", "missing"],
    )

    assert result.exit_code == 1
    assert "Perfil de tradução não encontrado." in result.output
    assert "Use --profile com um perfil definido" in result.output
    assert "Traceback" not in result.output
    assert not called


def test_translate_metadata_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--translate-metadata"],
    )

    assert result.exit_code == 0
    assert calls["options"].translate_metadata


def test_translate_alt_text_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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


def test_translate_chapters_option_reports_invalid_expression(minimal_epub_path, monkeypatch):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run for invalid chapter selection")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "3-1"],
    )

    assert result.exit_code == 1
    assert "Seleção de capítulos inválida." in result.output
    assert "chapter range is reversed" in result.output
    assert "Traceback" not in result.output


def test_translate_chapters_option_reports_unmatched_selection(minimal_epub_path, monkeypatch):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run when no chapter matches")

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "999"],
    )

    assert result.exit_code == 1
    assert "Nenhum capítulo corresponde à seleção informada." in result.output
    assert "no chapters matched selection" in result.output
    assert "Traceback" not in result.output


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

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(app, ["--mode", "developer", "translate", str(epub_path)])

    assert result.exit_code == 0
    assert "Idioma do EPUB ausente ou inválido" in result.output
    assert calls["options"].source == "en"


def test_translate_common_mode_shows_detected_source_hint(minimal_epub_path, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
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


def _mock_pipeline_with_route(monkeypatch, route):
    from ayvu.epub_io import TranslationReport as _Report

    def fake_preflight(**_kwargs: object) -> object:
        return SimpleNamespace(translator=object(), glossary=None, route=route)

    def fake_translate(input_path, output_path, **kwargs) -> _Report:
        return _Report(
            output_path=output_path,
            input_path=input_path,
            detected_language=kwargs["options"].source,
            target_language=kwargs["options"].target,
        )

    monkeypatch.setattr("ayvu.cli.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.cli.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.cli._offer_markdown_report", lambda *_args, **_kwargs: None)


def test_translate_developer_mode_prints_direct_route(minimal_epub_path, tmp_path, monkeypatch):
    from ayvu.translator import TranslationRoute

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(monkeypatch, TranslationRoute(source="en", target="pt"))

    result = runner.invoke(app, ["--mode", "developer", "translate", str(minimal_epub_path)])

    assert result.exit_code == 0
    assert "Route: en -> pt" in result.output
    assert "intermediário" not in result.output


def test_translate_developer_mode_prints_intermediate_route_with_warning(
    minimal_epub_path, tmp_path, monkeypatch
):
    from ayvu.translator import TranslationRoute

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(
        monkeypatch, TranslationRoute(source="fr", target="pt", intermediate="en")
    )

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--source", "fr"],
    )

    assert result.exit_code == 0
    assert "Route: fr -> en -> pt" in result.output
    assert "qualidade pode ficar comprometida" in result.output


def test_translate_common_mode_warns_about_intermediate_route(
    minimal_epub_path, tmp_path, monkeypatch
):
    from ayvu.translator import TranslationRoute

    monkeypatch.setattr("ayvu.cli.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(
        monkeypatch, TranslationRoute(source="fr", target="pt", intermediate="en")
    )

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(minimal_epub_path), "--source", "fr"],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "A tradução passará por 2 etapas" in result.output
    assert "fr -> en -> pt" in result.output
    assert "qualidade pode ficar comprometida" in result.output


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


def test_apply_review_command_rejects_v1_before_opening_epub_or_reconstructing(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = _write_review_csv_file(tmp_path / "review.csv")

    def fail_apply(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy v1 must be rejected before apply")

    monkeypatch.setattr("ayvu.cli.validate_output", fail_apply)

    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "CSV v1 serve apenas para inspeção" in result.output
    assert "legacy_reexport_required" in result.output
    assert "pacote v2" in result.output
    assert "Traceback" not in result.output


def test_apply_review_command_requires_a_v2_mapper_for_nonlegacy_input(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = tmp_path / "package.csv"
    review_path.write_text("placeholder", encoding="utf-8")
    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "invalid_preamble" in result.output
    assert "Traceback" not in result.output


def test_apply_review_command_rejects_legacy_before_output_conflict_check(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = _write_review_csv_file(tmp_path / "review.csv")
    output_path = tmp_path / "book-pt-reviewed.epub"
    output_path.write_bytes(b"already here")

    def fail_apply(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("apply should not run when output already exists")

    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "legacy_reexport_required" in result.output
    assert output_path.read_bytes() == b"already here"
    assert "Traceback" not in result.output


def test_apply_review_command_reports_unreadable_review_file(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    broken_csv = tmp_path / "broken.csv"
    broken_csv.write_text("segment_id,translated\nc0001-s0001,x\n", encoding="utf-8")

    result = runner.invoke(app, ["apply-review", str(epub_path), str(broken_csv)])

    assert result.exit_code == 1
    assert "Não foi possível ler o arquivo de revisão." in result.output
    assert "Traceback" not in result.output


def test_cli_console_width_does_not_depend_on_the_environment():
    from ayvu.cli import console

    assert console.width == 500


def _fake_http(monkeypatch, *, get=(), post=()):
    import requests as requests_module

    class Session:
        def __init__(self):
            self.calls = []
            self.get_script, self.post_script = list(get), list(post)

        def _next(self, script):
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def get(self, url, *, timeout):
            self.calls.append(("GET", url))
            return self._next(self.get_script)

        def post(self, url, *, json, timeout):
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


def test_test_translator_sends_only_the_known_text_and_reports_success(monkeypatch):
    session = _fake_http(monkeypatch, post=[_http(200, '{"translatedText": "Ola mundo"}')])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 0 and "Translator OK: Hello world -> Ola mundo" in result.output
    assert session.calls == [("POST", "http://localhost:5000/translate", {"q": "Hello world", "source": "en", "target": "pt", "format": "text"})]


def test_test_translator_help_discloses_the_text_it_sends():
    result = runner.invoke(app, ["test-translator", "--help"])
    assert result.exit_code == 0 and "Hello world" in result.output


def test_test_translator_treats_an_empty_translation_as_a_failure(monkeypatch):
    _fake_http(monkeypatch, post=[_http(200, '{"translatedText": ""}')])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "Translator OK" not in result.output and "empty_result" in result.output


@pytest.mark.parametrize("body", ["bad language pair SECRETBODY", "Call me Ishmael."])
def test_test_translator_failure_shows_a_code_and_not_the_response(monkeypatch, body):
    _fake_http(monkeypatch, post=[_http(400, body)])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "O teste do tradutor falhou." in result.output
    assert "provider_rejected" in result.output and body not in result.output


def test_common_mode_failure_shows_no_technical_detail(monkeypatch):
    _fake_http(monkeypatch, post=[_http(500, "oops")])
    result = runner.invoke(app, ["--mode", "common", "test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "Detalhe técnico" not in result.output and "provider_server_error" not in result.output


def test_test_translator_refuses_an_invalid_language_before_sending(monkeypatch):
    session = _fake_http(monkeypatch)
    result = runner.invoke(app, ["test-translator", "--source", "x y"])
    assert result.exit_code == 1 and "invalid_locale" in result.output and session.calls == []


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


def test_languages_keeps_the_table_and_only_asks_for_languages(monkeypatch):
    session = _fake_http(monkeypatch, get=[_http(200, '[{"code": "pt", "name": "Portuguese", "targets": ["en", "es"]}]')])
    result = runner.invoke(app, ["languages", "--retries", "0"])
    assert result.exit_code == 0 and "Portuguese" in result.output and "en, es" in result.output and "installed" in result.output
    assert session.calls == [("GET", "http://localhost:5000/languages")]


def test_languages_with_an_empty_list_still_succeeds(monkeypatch):
    _fake_http(monkeypatch, get=[_http(200, "[]")])
    result = runner.invoke(app, ["languages", "--retries", "0"])
    assert result.exit_code == 0 and "LibreTranslate languages" in result.output


def test_guided_selection_warns_with_a_code_and_not_the_response(monkeypatch, capsys):
    from ayvu import cli as cli_module

    _fake_http(monkeypatch, get=[_http(500, "SECRETBODY")] * 2)
    assert cli_module._load_languages_for_guided_selection() == ()
    output = capsys.readouterr().out
    assert "provider_server_error" in output and "SECRETBODY" not in output
