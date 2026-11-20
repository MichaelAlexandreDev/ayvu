"""Real production composition and stores; only the HTTP transport boundary is faked."""
from __future__ import annotations
import logging
import sqlite3
from zipfile import ZipFile
import pytest
from ayvu.interfaces.cli import app as cli
import ayvu.composition.builders.translate_project as composition
from tests.support.legacy_cache_file import CacheKey, LegacyCacheFile
from ayvu.jobs.model import JobState
from ayvu.review import decode_review_package
from tests.support.cli_translate_project_support import _book, _jobs
from ayvu.kernel import LanguagePair
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, records
from tests.support.cli_rich_runtime_support import runner, text


def test_rich_translate_real_stores_cache_and_review_with_instrumented_transport(tmp_path, monkeypatch, caplog):
    label = "private-model-canary-252"
    config(tmp_path, label)
    source = _book(tmp_path / "book.epub")
    original = source.read_bytes()
    cache = tmp_path / "cache.sqlite"
    with LegacyCacheFile(cache) as legacy:
        legacy.set(CacheKey("Hello title", LanguagePair(source="en", target="pt")), "LEGACY_ONLY")
    v1 = cache.read_bytes()
    with caplog.at_level(logging.DEBUG):
        first, transport = invoke(tmp_path, monkeypatch, source, "--review-output", str(tmp_path / "review.csv"))
    assert first.exit_code == 0, text(first)
    assert "Translation report" in text(first) and "Review file saved to:" in text(first)
    assert transport.posts == ["T", "Hello title", "Hello reader.", "Goodbye reader."]
    assert [(method, url) for method, url, _ in transport.requests] == [
        ("GET", "http://localhost:5000/languages"), *[("POST", "http://localhost:5000/translate")] * 4]
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == "translate_project")
    assert job.state is JobState.VALIDATED and job.request.command_version == 2
    assert "raw_results" in job.request.output_roles
    provenance = records(tmp_path, job)
    assert provenance and all(record.producer.model == "declared:" + label for record in provenance)
    assert any(record.operation == "execution_identity" for record in provenance)
    package = decode_review_package((tmp_path / "review.csv").read_bytes())
    assert len(package.rows) > 0 and all(row.machine_translation for row in package.rows)
    assert label not in caplog.text + repr(job) + repr(provenance)
    assert label in text(first)  # #377 deliberately presents the selected model.
    with ZipFile(tmp_path / "out.epub") as output:
        assert b"PT:Hello reader." in output.read("OEBPS/c1.xhtml")
    second, transport = invoke(tmp_path, monkeypatch, source, "--overwrite")
    assert second.exit_code == 0, text(second)
    assert transport.posts == [] and [method for method, _, _ in transport.requests] == ["GET"]
    assert cache.read_bytes() == v1 and source.read_bytes() == original
    with sqlite3.connect(composition.v2_cache_path(cache)) as connection:
        assert connection.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (4,)


def test_changing_declared_model_misses_and_restoring_hits_real_v2(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    for index, (label, calls) in enumerate([("a", 4), ("b", 4), ("a", 0)]):
        config(tmp_path, label)
        result, session = invoke(tmp_path, monkeypatch, source, *( ["--overwrite"] if index else []))
        assert result.exit_code == 0, text(result)
        assert len(session.posts) == calls


@pytest.mark.parametrize("index", [0, 1, 3])
def test_rich_provider_failure_first_middle_last_is_resumable_without_partial_epub(tmp_path, monkeypatch, index):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    texts = ["T", "Hello title", "Hello reader.", "Goodbye reader."]
    result, transport = invoke(tmp_path, monkeypatch, source, session=Transport(failing=texts[index]))
    assert result.exit_code == 1 and "nenhum EPUB parcial" in " ".join(text(result).split())
    assert transport.posts == texts[:index + 1]
    assert not (tmp_path / "out.epub").exists()
    from ayvu.recovery.checkpoint import ResumeStateStore
    checkpoint = ResumeStateStore(tmp_path / "processing").scan().running[0]
    assert checkpoint.execution_revision == 2
    assert "local-model-2026" not in repr(checkpoint.to_dict())
    parent = _jobs(tmp_path)[0]
    assert parent.state is JobState.FAILED and parent.terminal_summary.reason_code == "provider_server_error"
    resumed = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: resumed)
    result = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    assert result.exit_code == 0, text(result)
    assert resumed.posts == texts[index:]
    assert any(job.continuation_of == parent.job_id and job.state is JobState.VALIDATED for job in _jobs(tmp_path))


def test_resuming_a_rich_job_without_a_configured_label_recovers_the_original(tmp_path, monkeypatch):
    # A v1 job resumed after a label is declared finishes on v1 instead: test_cli_resume_runtime.py.
    config(tmp_path, "model")
    source = _book(tmp_path / "book.epub")
    def interrupt(_):
        raise KeyboardInterrupt
    failed, _ = invoke(tmp_path, monkeypatch, source, session=Transport(on_post=interrupt))
    assert failed.exit_code == 1
    original_jobs = _jobs(tmp_path)
    config(tmp_path, None)
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    resumed = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    output = text(resumed)
    assert resumed.exit_code == 0, output
    assert transport.requests
    assert len(_jobs(tmp_path)) == len(original_jobs) + 1
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == original_jobs[-1].job_id)
    assert all(record.producer.model == 'declared:model' for record in records(tmp_path, child))
