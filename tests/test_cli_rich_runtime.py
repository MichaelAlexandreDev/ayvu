"""Real production composition and stores; only the HTTP transport boundary is faked."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

import ayvu.cli as cli
import ayvu.composition.builders.translate_project as composition
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.preferences.model import AyvuConfig
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobEventSequence, encode_event
from ayvu.preferences.model import PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.review_exchange import decode_review_package
from ayvu.translate_project import ContinuationTicket, TRANSLATE_BEFORE_START, TranslateObserver
from ayvu.translation_engine import ENGINE_SAFE_POINTS
from test_cli_translate_project import Session, _args, _book, _jobs
from test_translate_project import CancelAt
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions, JobWarning

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_processing_dir", lambda _: tmp_path / "processing")


class Transport(Session):
    """Records every method, URL and payload; no socket is used."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, None))
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, dict(kwargs["json"])))
        return super().post(url, **kwargs)


def config(tmp_path, label="local-model-2026", **values):
    path = tmp_path / "config" / "ayvu" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"books_dir": str(tmp_path / "books"), **values}
    if label is not None:
        data["translator_models"] = {"libretranslate": label}
    path.write_text(json.dumps(data))
    return path


def invoke(tmp_path, monkeypatch, source, *arguments, session=None, output=None):
    session = session or Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: session)
    result = runner.invoke(cli.app, _args(tmp_path, source, *arguments, output=output))
    return result, session


def text(result):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)


def records(tmp_path, job):
    paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(job.request.project_id)
    return [node.record.request for node in open_provenance_repository(paths.database, paths.backups,
        job.request.project_id, clock=lambda: datetime.now(UTC)).list_by_job(job.job_id, limit=1024).nodes]


def test_rich_translate_real_stores_cache_and_review_with_instrumented_transport(tmp_path, monkeypatch, caplog):
    label = "private-model-canary-252"
    config(tmp_path, label)
    source = _book(tmp_path / "book.epub")
    original = source.read_bytes()
    cache = tmp_path / "cache.sqlite"
    with TranslationCache(cache) as legacy:
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
    assert label not in caplog.text + text(first) + repr(job) + repr(provenance)
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
    from ayvu.v1.resume import ResumeStateStore
    checkpoint = ResumeStateStore(tmp_path / "processing").scan().running[0]
    assert checkpoint.execution_revision == 2
    assert "local-model-2026" not in repr(checkpoint.to_dict())
    parent = _jobs(tmp_path)[0]
    assert parent.state is JobState.FAILED and parent.terminal_summary.reason_code == "provider_server_error"
    resumed = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: resumed)
    result = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    assert result.exit_code == 0, text(result)
    assert resumed.posts == texts[index:]
    assert any(job.continuation_of == parent.job_id and job.state is JobState.VALIDATED for job in _jobs(tmp_path))


@pytest.mark.parametrize("original_label,next_label", [(None, "model"), ("model", None)])
def test_resume_across_runtimes_is_refused_and_names_both_before_any_request(tmp_path, monkeypatch, original_label, next_label):
    config(tmp_path, original_label)
    source = _book(tmp_path / "book.epub")
    def interrupt(_):
        raise KeyboardInterrupt
    failed, _ = invoke(tmp_path, monkeypatch, source, session=Transport(on_post=interrupt))
    assert failed.exit_code == 1
    original_jobs = _jobs(tmp_path)
    config(tmp_path, next_label)
    transport = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: transport)
    resumed = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    output = text(resumed)
    assert resumed.exit_code == 1 and "legacy/v1" in output and "rich/v2" in output
    assert transport.requests == []
    assert len(_jobs(tmp_path)) == len(original_jobs)


@pytest.mark.parametrize("arguments,gate", [(["--workers", "2"], "--workers 1"),
    (["--cache-only"], "--cache-only"),
    (["--cache-only", "--require-full-cache"], "--require-full-cache")])
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
    monkeypatch.setattr(cli, "configuration_store", lambda: SimpleNamespace(get_preferences=lambda: PreferencesSnapshot(
        None, None, True, error=PreferencesError("invalid", "translator_models"))))
    result, session = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"))
    assert result.exit_code == 1 and "Rótulo de modelo inválido" in text(result)
    assert session.requests == []


def test_runtime_warning_reaches_the_cli(tmp_path, monkeypatch):
    from uuid import UUID
    from ayvu.jobs.events import JobCorrelation, JobEvent
    from test_cli_translate_project import _canned, _report, _result
    event = JobEvent(str(UUID(int=1)), str(UUID(int=2)), 1, datetime.now(UTC),
                     JobCorrelation(str(UUID(int=3))), JobWarning("provider_advisory"))
    result = _result(JobState.VALIDATED, "validated", events=(event,))
    invocation = _canned(tmp_path, monkeypatch, result, _report)
    assert invocation.exit_code == 0, text(invocation)
    assert "Aviso da tradução:" in text(invocation) and "provider_advisory" in text(invocation)


@pytest.mark.parametrize("arguments", [["--dry-run"], ["--dry-run", "--workers", "2"]])
def test_declared_model_does_not_select_rich_for_dry_run(tmp_path, monkeypatch, arguments):
    config(tmp_path)
    result, session = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), *arguments)
    assert result.exit_code == 0, text(result)
    assert session.requests == [] and not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()
    assert _jobs(tmp_path) == []


def test_preview_stays_on_v1_with_a_declared_model(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    session = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: session)
    result = runner.invoke(cli.app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 0, text(result)
    assert (tmp_path / "books" / "Preview" / "book-preview.epub").exists()
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists() and _jobs(tmp_path) == []


def test_guided_translation_selects_rich_from_existing_configuration(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    transport = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: transport)
    result = runner.invoke(cli.app, [], input=f"1\n{source}\n1\ny\nn\n")
    assert result.exit_code == 0, text(result)
    job = _jobs(tmp_path)[0]
    assert job.request.command_version == 2 and job.state is JobState.VALIDATED
    assert (tmp_path / "books" / "Traduzidos" / "book-pt.epub").exists()
    assert composition.v2_cache_path(cli.DEFAULT_CACHE_PATH).exists()


def test_each_batch_book_uses_rich_and_keeps_markdown_reports(tmp_path, monkeypatch):
    config(tmp_path)
    first = _book(tmp_path / "first.epub")
    second = _book(tmp_path / "second.epub", chapter_two="<p>A different ending.</p>")
    transport = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: transport)
    result = runner.invoke(cli.app, ["--mode", "developer", "translate", str(first), str(second), "--source", "en", "--target", "pt",
        "--cache", str(tmp_path / "cache.sqlite"), "--output-dir", str(tmp_path / "output"), "--retries", "0"])
    assert result.exit_code == 0, text(result)
    jobs = _jobs(tmp_path)
    assert len(jobs) == 2 and all(job.request.command_version == 2 and job.state is JobState.VALIDATED for job in jobs)
    assert len(list((tmp_path / "output").glob("*.epub"))) == 2
    reports = list((tmp_path / "books" / "Relatorios").glob("*.md"))
    assert len(reports) == 2 and all("Translation report" in path.read_text() for path in reports)


@pytest.mark.parametrize("denial", ["none", "loopback"])
def test_rich_network_policy_over_instrumented_transport(tmp_path, monkeypatch, denial):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    args = ["--network-policy", denial]
    if denial == "loopback":
        args += ["--url", "http://remote.invalid:5000"]
    result, transport = invoke(tmp_path, monkeypatch, source, *args)
    assert result.exit_code == 1
    assert transport.requests == []
    assert ("strict_offline" if denial == "none" else "unsupported_destination") in text(result)


@pytest.mark.parametrize("point", tuple(dict.fromkeys((TRANSLATE_BEFORE_START, *ENGINE_SAFE_POINTS,
    *composition.epub_format_service().safe_points("epub"), *composition.DOCUMENT_SAFE_POINTS))))
def test_rich_job_cancellation_at_each_existing_safe_point(tmp_path, monkeypatch, point):
    source = _book(tmp_path / "book.epub")
    projects = ProjectJsonRepository(tmp_path / "processing")
    project = projects.resolve(build_project_request(source, source="en", target="pt")).project
    session = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: session)
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    def prepare():
        from ayvu.preflight import run_translation_preflight
        prepared = run_translation_preflight(source, tmp_path / "cache.sqlite", None, "libretranslate", "http://localhost:5000",
            30, 0, options.language_pair, False, cache_check=lambda _: None)
        return composition.LegacyResources(prepared.translator, prepared.glossary)
    paths = projects.resolve_database_paths(project.project_id)
    runtime = composition.RichTranslationRuntime(source, options, prepare, tmp_path / "cache.sqlite", "model",
        paths.database, paths.backups, projects.resolve_artifact_root(project.project_id), project.project_id)
    use_case = composition.open_translate_project(tmp_path / "processing", project.project_id, source, tmp_path / "out.epub",
        overwrite=False, runtime=runtime)
    points = tuple(dict.fromkeys((TRANSLATE_BEFORE_START, *ENGINE_SAFE_POINTS, *composition.epub_format_service().safe_points("epub"),
                                 *composition.DOCUMENT_SAFE_POINTS)))
    cancel = CancelAt(points, point)
    from ayvu.translate_project import TranslateProjectCommand
    command = TranslateProjectCommand(project.project_id, project.source_sha256, options,
        OutputPlan.for_translation(source, tmp_path / "out.epub", options.language_pair), execution_revision=2, producer=runtime.producer,
        additional_output_roles=("raw_results",))
    result = use_case.execute(command, cancellation=cancel)
    assert cancel.reached and cancel.acknowledgement.safe_point == point
    assert result.state is JobState.CANCELLED and result.outcome.summary.reason_code == "cancelled"
    assert not (tmp_path / "out.epub").exists()
    sequence = JobEventSequence(result.job_id)
    for event in result.events:
        sequence.accept(event)
    assert sequence.outcome is result.outcome



def test_tm_approved_epub_workflow_and_cache_clear_isolation_real_stores(tmp_path, monkeypatch):
    from uuid import uuid4
    from ayvu.knowledge.memory.approved.model import Candidate, MemoryScope, MemorySource, MemoryState, ScopeKind, TargetValue, ValueKind, PipelineStage
    from ayvu.knowledge.memory.approved.service import CreateCandidate, ReviewRevision
    from ayvu.knowledge.memory.approved.store import open_approved_memory
    from ayvu.translation.stages.memory_retrieval import plan_memory_pin
    from ayvu.provenance.model import ActorKind, ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef
    config(tmp_path)
    source = _book(tmp_path / 'book.epub')
    original_bytes = source.read_bytes()
    seen = []
    real_translate = composition.DocumentTranslator.translate
    def observe(self, document, *args, **kwargs):
        seen.append((document, self._plan))
        return real_translate(self, document, *args, **kwargs)
    monkeypatch.setattr(composition.DocumentTranslator, 'translate', observe)
    first, transport = invoke(tmp_path, monkeypatch, source, '--translation-memory')
    assert first.exit_code == 0, text(first)
    assert transport.posts == ['T', 'Hello title', 'Hello reader.', 'Goodbye reader.']
    document, plan = next((doc, plan) for doc, plan in seen if any(u.source_text == 'Hello reader.' for u in doc.units))
    selected = next(u for u in document.units if u.source_text == 'Hello reader.')
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == 'translate_project')
    paths = ProjectJsonRepository(tmp_path / 'processing').resolve_database_paths(document.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, document.project_id)
    scope = MemoryScope(ScopeKind.PROJECT, document.project_id)
    repository = open_approved_memory(paths.database.parent, allowed_scopes=frozenset({scope}),
        provenance={document.project_id: provenance})
    pin = plan_memory_pin(plan)
    def evidence(operation, operation_id, actor, parents=()):
        return provenance.append(ProvenanceRequest(document.project_id, job.job_id, operation, actor,
            SourceKind.HUMAN if actor is ActorKind.HUMAN else SourceKind.PIPELINE,
            ProducerIdentity('not_applicable', 'not_applicable', 'not_applicable', 'not_applicable'),
            subject=SubjectRef('content_unit', 1, selected.unit_id), configuration_refs=pin.references,
            parents=parents, operation_id=operation_id))
    operation_id = str(uuid4())
    created = repository.create(CreateCandidate(operation_id, Candidate(
        MemorySource.from_document(document, selected.unit_id), LanguagePair('en', 'pt'),
        TargetValue('REVISADO', ValueKind.LINGUISTIC_FINAL, PipelineStage.LINGUISTIC_FINAL, pin), scope,
        evidence('tm_create', operation_id, ActorKind.SYSTEM).record_id)))
    operation_id = str(uuid4())
    approved = repository.review(ReviewRevision(operation_id, created.revision_id, created.event.version,
        scope, evidence('tm_approve', operation_id, ActorKind.HUMAN, (created.event.provenance_id,)).record_id,
        MemoryState.APPROVED, created.value.target))
    memory_file = paths.database.parent / 'approved-memory.sqlite'
    memory_bytes = memory_file.read_bytes()
    # Clear through the real cache administration boundary, not through the TM repository.
    cleaned = runner.invoke(cli.app, ['cache', 'clean', '--cache', str(composition.v2_cache_path(tmp_path / 'cache.sqlite')),
        '--namespace', 'v2', '--scope-kind', 'project', '--scope-id', document.project_id,
        '--project-id', document.project_id, '--all', '--yes'])
    assert cleaned.exit_code == 0, text(cleaned)
    assert memory_file.read_bytes() == memory_bytes
    second, transport = invoke(tmp_path, monkeypatch, source, '--translation-memory', '--overwrite')
    assert second.exit_code == 0, text(second)
    assert transport.posts == ['T', 'Hello title', 'Goodbye reader.']
    assert 'Texts from memory' in text(second)
    with ZipFile(tmp_path / 'out.epub') as output:
        assert b'REVISADO' in output.read('OEBPS/c1.xhtml')
    assert source.read_bytes() == original_bytes
    assert repository.get(approved.revision_id, scopes=(scope,)) == approved


def test_tm_empty_repository_provider_failure_and_resume_use_real_boundaries(tmp_path, monkeypatch):
    from ayvu.v1.resume import ResumeStateStore
    config(tmp_path)
    source = _book(tmp_path / 'book.epub')
    first, transport = invoke(tmp_path, monkeypatch, source, '--translation-memory', session=Transport(failing='Goodbye reader.'))
    assert first.exit_code == 1 and transport.posts == ['T', 'Hello title', 'Hello reader.', 'Goodbye reader.'], text(first)
    checkpoint = ResumeStateStore(tmp_path / 'processing').scan().running[0]
    assert checkpoint.translation_memory_enabled
    assert not checkpoint.translation_memory_options().legacy_cache_compatibility
    resumed_transport = Transport()
    monkeypatch.setattr('ayvu.v1.translator.requests.Session', lambda: resumed_transport)
    result = runner.invoke(cli.app, ['--mode', 'developer', 'resume', str(source)])
    assert result.exit_code == 0, text(result)
    assert resumed_transport.posts == ['Goodbye reader.']
    job = _jobs(tmp_path)[-1]
    paths = ProjectJsonRepository(tmp_path / 'processing').resolve_database_paths(job.request.project_id)
    with sqlite3.connect(paths.database.parent / 'approved-memory.sqlite') as db:
        assert db.execute('SELECT count(*) FROM tm_revisions').fetchone() == (0,)


@pytest.mark.parametrize('flag', ['--tm-conservative-auto-apply', '--tm-legacy-cache-compatibility'])
def test_new_tm_policy_flags_require_explicit_memory_before_any_transport(tmp_path, monkeypatch, flag):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / 'book.epub'), flag)
    assert result.exit_code == 1 and 'requerem --translation-memory' in ' '.join(text(result).split())
    assert transport.requests == []


def test_cli_tm_conservative_threshold_gate_is_explicit_and_precedes_transport(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / 'book.epub'),
        '--translation-memory', '--tm-conservative-auto-apply', '--tm-apply-threshold', '0.95')
    assert result.exit_code == 1 and '0.98' in text(result)
    assert transport.requests == []



def test_memory_review_warning_reaches_current_cli_presentation(tmp_path, monkeypatch):
    from uuid import UUID
    from ayvu.jobs.events import JobCorrelation, JobEvent
    from test_cli_translate_project import _canned, _report, _result
    event = JobEvent(str(UUID(int=1)), str(UUID(int=2)), 1, datetime.now(UTC),
        JobCorrelation(str(UUID(int=3))), JobWarning('memory_review_suggestions'))
    result = _result(JobState.VALIDATED, 'validated', events=(event,))
    invocation = _canned(tmp_path, monkeypatch, result, _report)
    assert invocation.exit_code == 0, text(invocation)
    assert 'Aviso da tradução:' in text(invocation) and 'memory_review_suggestions' in text(invocation)
