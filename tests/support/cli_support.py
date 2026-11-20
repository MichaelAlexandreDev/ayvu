import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
from ebooklib import epub
from typer.testing import CliRunner
from tests.support.legacy_cache_file import CacheKey, LegacyCacheFile
from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.interfaces.cli.app import app
from ayvu.composition.cli import _BookOutcome
from ayvu.translation import TranslationReport
from ayvu.jobs.model import JobState
from ayvu.translation import HtmlTranslationStats
from ayvu.translation.preflight import PreflightError
from ayvu.recovery.checkpoint import COMPLETED_STATUS, ResumeStateStore, TranslationResumeState
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

    The real run through the use case is in tests/interfaces/cli/test_workflow_translation_resume.py and tests/interfaces/cli/test_cli_project_translation.py.
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
    with LegacyCacheFile(cache_path) as cache:
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


def _library_config(isolated_config, books_dir, reader_app=None):
    data = {"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}
    if reader_app:
        data["reader_app"] = reader_app
    isolated_config.write_text(json.dumps(data) + "\n", encoding="utf-8")


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
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)


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
