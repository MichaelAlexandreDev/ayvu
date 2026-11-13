from __future__ import annotations
from datetime import UTC, datetime
import pytest
from ayvu.interfaces.cli import app as cli, settings as cli_settings, options as cli_options
import ayvu.composition.builders.translate_project as composition
from ayvu.jobs.model import JobState
from ayvu.preferences.model import PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from tests.support.cli_translate_project_support import _book, _jobs, _args
from ayvu.kernel import JobWarning
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate
from tests.support.cli_rich_runtime_support import runner, text


@pytest.mark.parametrize("arguments,gate", [(["--workers", "2"], "--workers 1"),
    (["--cache-only", "--workers", "2"], "--workers 1")])
def test_cli_unsupported_rich_options_name_the_actual_refusal(tmp_path, monkeypatch, arguments, gate):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), *arguments)
    assert result.exit_code == 1 and gate in text(result) and "rich/v2" in text(result)
    assert transport.requests == [] and not (tmp_path / "out.epub").exists()


def test_require_full_cache_without_cache_only_keeps_its_original_cli_gate(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--require-full-cache")
    assert result.exit_code == 1 and "--cache-only" in text(result)
    assert transport.requests == [] and _jobs(tmp_path) == []


def test_invalid_label_configuration_refused_before_any_provider(tmp_path, monkeypatch):
    config(tmp_path, "https://PRIVATE-LABEL")
    result, session = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"))
    assert result.exit_code == 1 and "Rótulo de modelo inválido" in text(result)
    assert "PRIVATE-LABEL" not in result.output and session.requests == []
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


def test_invalid_label_presentation_gate_even_when_storage_is_unavailable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(cli_settings, "configuration_store", lambda: SimpleNamespace(get_preferences=lambda: PreferencesSnapshot(None, None, True, error=PreferencesError("invalid", "translator_models"))))
    result, session = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"))
    assert result.exit_code == 1 and "Rótulo de modelo inválido" in text(result)
    assert session.requests == []


def test_runtime_warning_reaches_the_cli(tmp_path, monkeypatch):
    from uuid import UUID
    from ayvu.jobs.events import JobCorrelation, JobEvent
    from tests.support.cli_translate_project_support import _canned, _report, _result
    event = JobEvent(str(UUID(int=1)), str(UUID(int=2)), 1, datetime.now(UTC),
                     JobCorrelation(str(UUID(int=3))), JobWarning("provider_advisory"))
    result = _result(JobState.VALIDATED, "validated", events=(event,))
    invocation = _canned(tmp_path, monkeypatch, result, _report)
    assert invocation.exit_code == 0, text(invocation)
    assert "Aviso da tradução:" in text(invocation) and "provider_advisory" in text(invocation)


@pytest.mark.parametrize("arguments", [["--dry-run"], ["--dry-run", "--workers", "2"]])
def test_declared_model_selects_transient_rich_for_dry_run(tmp_path, monkeypatch, arguments):
    from ayvu.composition.builders import translate_transient
    from tests.support.transient_translation_support import effect_guard
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    selected = []
    original = translate_transient.translate
    def translate(book, hooks, label, **kwargs):
        selected.append(label)
        return original(book, hooks, label, **kwargs)
    monkeypatch.setattr(translate_transient, "translate", translate)
    with effect_guard(monkeypatch, tmp_path):
        result = runner.invoke(cli.app, _args(tmp_path, source, *arguments))
    assert result.exit_code == 0, text(result)
    assert selected == ["local-model-2026"] and not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()
    assert _jobs(tmp_path) == []


def test_preview_stays_on_v1_with_a_declared_model(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    result = runner.invoke(cli.app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 0, text(result)
    assert (tmp_path / "books" / "Preview" / "book-preview.epub").exists()
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists() and _jobs(tmp_path) == []


def test_guided_translation_selects_rich_from_existing_configuration(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    result = runner.invoke(cli.app, [], input=f"1\n{source}\n1\ny\nn\n")
    assert result.exit_code == 0, text(result)
    job = _jobs(tmp_path)[0]
    assert job.request.command_version == 2 and job.state is JobState.VALIDATED
    assert (tmp_path / "books" / "Traduzidos" / "book-pt.epub").exists()
    assert composition.v2_cache_path(cli_options.DEFAULT_CACHE_PATH).exists()


def test_each_batch_book_uses_rich_and_keeps_markdown_reports(tmp_path, monkeypatch):
    config(tmp_path)
    first = _book(tmp_path / "first.epub")
    second = _book(tmp_path / "second.epub", chapter_two="<p>A different ending.</p>")
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    result = runner.invoke(cli.app, ["--mode", "developer", "translate", str(first), str(second), "--source", "en", "--target", "pt",
        "--cache", str(tmp_path / "cache.sqlite"), "--output-dir", str(tmp_path / "output"), "--retries", "0"])
    assert result.exit_code == 0, text(result)
    jobs = _jobs(tmp_path)
    assert len(jobs) == 2 and all(job.request.command_version == 2 and job.state is JobState.VALIDATED for job in jobs)
    assert len(list((tmp_path / "output").glob("*.epub"))) == 2
    reports = list((tmp_path / "books" / "Relatorios").glob("*.md"))
    assert len(reports) == 2 and all("Translation report" in path.read_text() for path in reports)
