from __future__ import annotations
import sqlite3
from datetime import UTC, datetime
from zipfile import ZipFile
import pytest
from ayvu.interfaces.cli import app as cli
import ayvu.composition.builders.translate_project as composition
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobEventSequence
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.translation.project_contracts import TRANSLATE_BEFORE_START
from ayvu.translation.engine import ENGINE_SAFE_POINTS
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.translate_project_support import CancelAt
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions, JobWarning
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate
from tests.support.cli_rich_runtime_support import runner, text


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
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    def prepare():
        from ayvu.translation.preflight import run_translation_preflight
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
    from ayvu.translation.project_contracts import TranslateProjectCommand
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
    from ayvu.translation import DocumentTranslator
    real_translate = DocumentTranslator.translate
    def observe(self, document, *args, **kwargs):
        seen.append((document, self._plan))
        return real_translate(self, document, *args, **kwargs)
    monkeypatch.setattr(DocumentTranslator, 'translate', observe)
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
    from ayvu.recovery.checkpoint import ResumeStateStore
    config(tmp_path)
    source = _book(tmp_path / 'book.epub')
    first, transport = invoke(tmp_path, monkeypatch, source, '--translation-memory', session=Transport(failing='Goodbye reader.'))
    assert first.exit_code == 1 and transport.posts == ['T', 'Hello title', 'Hello reader.', 'Goodbye reader.'], text(first)
    checkpoint = ResumeStateStore(tmp_path / 'processing').scan().running[0]
    assert checkpoint.translation_memory_enabled
    assert not checkpoint.translation_memory_options().legacy_cache_compatibility
    resumed_transport = Transport()
    monkeypatch.setattr('ayvu.providers.libretranslate.client.requests.Session', lambda: resumed_transport)
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
    from tests.support.cli_translate_project_support import _canned, _report, _result
    event = JobEvent(str(UUID(int=1)), str(UUID(int=2)), 1, datetime.now(UTC),
        JobCorrelation(str(UUID(int=3))), JobWarning('memory_review_suggestions'))
    result = _result(JobState.VALIDATED, 'validated', events=(event,))
    invocation = _canned(tmp_path, monkeypatch, result, _report)
    assert invocation.exit_code == 0, text(invocation)
    assert 'Aviso da tradução:' in text(invocation) and 'memory_review_suggestions' in text(invocation)
