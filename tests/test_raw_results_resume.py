"""#242: a continuation replays an earlier job's raw results only when every producing dimension is proved equal."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from typer.testing import CliRunner

import ayvu.cli as cli
import ayvu.composition.builders.translate_project as composition
from ayvu.job_execution import InMemoryJobEventLog
from ayvu.provenance.model import ProducerIdentity
from ayvu.provider import ProviderRoute
from ayvu.translation_document import (
    MAX_REUSABLE_JOBS, RAW_RESULTS_VERSION, DocumentTranslator, ExecutionContext, TranslationExecutionError, UnitSource,
    reuse_dimensions,
)
from ayvu.job_recovery import REUSE_DIMENSIONS
from test_translation_document import Env, Provider, cue, document, plan, rewrite_payload, upper_glossary
from ayvu.kernel import JobWarning

runner = CliRunner()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def continue_job(env, *ancestors, subject=None, subject_plan=None, provider=None):
    """A new running job of the same project, allowed to look back at ``ancestors``."""
    job = env.running_job()
    log = InMemoryJobEventLog(job.job_id)
    provider = provider or Provider(**({} if subject_plan is None else {'producer': subject_plan.producer, 'route': subject_plan.route}))
    translator = DocumentTranslator(subject_plan or plan(), provider, upper_glossary, env.artifacts, env.provenance, log,
                                    clock=env.clock, new_id=env.new_id)
    result = translator.translate(subject or document(), ExecutionContext(
        job, job.lease.owner_token, reusable_jobs=tuple(item.job_id for item in ancestors)))
    warnings = [event.payload.code for event in log.replay() if isinstance(event.payload, JobWarning)]
    return job, result, provider, warnings


def artifact_row(env, artifact_id):
    with closing(sqlite3.connect(env.database)) as connection:
        return connection.execute('SELECT state, revision, size, digest FROM job_artifacts WHERE artifact_id = ?',
                                  (artifact_id,)).fetchone()


def test_raw_results_record_every_reuse_dimension(env):
    first = env.translate()
    data = json.loads(env.final(first.artifact_id).read_bytes())
    assert data['schema_version'] == RAW_RESULTS_VERSION == 2
    assert tuple(data['dimensions']) == tuple(sorted(REUSE_DIMENSIONS))
    assert data['dimensions'] == reuse_dimensions(document(), plan())
    assert all(value.startswith('sha256:') and len(value) == 71 for value in data['dimensions'].values())


def test_a_continuation_replays_proven_raw_results_with_no_provider_call(env):
    first = env.translate()
    parent = env.job
    before = artifact_row(env, first.artifact_id)
    job, result, provider, warnings = continue_job(env, parent)
    assert provider.requests == [] and warnings == []
    assert [unit.source for unit in result.units] == [UnitSource.ARTIFACT_REPLAY] * 2
    assert [unit.text for unit in result.units] == [unit.text for unit in first.units]
    assert result.artifact_id == first.artifact_id
    # The earlier job's record is read, never changed, and the replay is the new job's own fact.
    assert artifact_row(env, first.artifact_id) == before
    replays = [node.record for node in env.provenance.list_by_job(job.job_id, limit=100).nodes
               if node.record.request.operation == 'replay_raw_results']
    assert len(replays) == 1 and replays[0].request.artifacts == (first.artifact_id,)
    assert env.jobs.get(parent.job_id).job == parent


def test_without_a_chain_another_job_still_cannot_replay(env):
    env.translate()
    _, result, provider, warnings = continue_job(env)
    assert len(provider.requests) == 2 and warnings == []
    assert {unit.source for unit in result.units} == {UnitSource.PROVIDER}


@pytest.mark.parametrize('changes,dimension', [
    ({'producer': ProducerIdentity('engine@1', 'model@2', 'adapter@1', 'not_applicable')}, 'provider'),
    ({'producer': ProducerIdentity('engine@1', 'model@1', 'adapter@1', 'pipeline@2')}, 'pipeline'),
    ({'route': ProviderRoute('en', 'pt', 'es')}, 'route'),
    ({'capabilities': 'capfp:v1:' + '2' * 64}, 'capability'),
    ({'chunk_limit': 50}, 'capability'),
], ids=['model', 'pipeline', 'route', 'capabilities', 'chunk-limit'])
def test_a_changed_dimension_is_named_and_the_work_is_redone(env, changes, dimension):
    env.translate()
    _, result, provider, warnings = continue_job(env, env.job, subject_plan=plan(**changes))
    assert len(provider.requests) == 2
    assert {unit.source for unit in result.units} == {UnitSource.PROVIDER}
    assert warnings == ['raw_results_incompatible_plan', f'raw_results_changed_{dimension}']


def test_a_plan_with_no_semantic_identity_never_reuses_and_says_nothing(env):
    env.translate()
    # A policy without its configuration revision leaves the plan unidentified, so nothing can be proved.
    unidentified = plan(policy_ref='policy:' + 'c' * 64)
    assert unidentified.semantic_fingerprint is None
    _, result, provider, warnings = continue_job(env, env.job, subject_plan=unidentified)
    assert len(provider.requests) == 2 and warnings == []
    assert {unit.source for unit in result.units} == {UnitSource.PROVIDER}


def test_another_document_revision_is_named_as_an_input_change(env):
    env.translate()
    changed = document(cue('a', ' Hello there ', 1), cue('b', 'Good evening', 2))
    _, result, provider, warnings = continue_job(env, env.job, subject=changed)
    assert len(provider.requests) == 2
    assert 'raw_results_changed_input' in warnings and 'raw_results_incompatible_plan' in warnings


def test_an_unrecorded_dimension_prevents_reuse_and_is_not_called_a_change(env):
    first = env.translate()

    def as_version_one(data):
        data.pop('dimensions')
        data['schema_version'] = 1
    rewrite_payload(env, first.artifact_id, as_version_one)
    _, result, provider, warnings = continue_job(env, env.job)
    assert len(provider.requests) == 2
    assert warnings == ['raw_results_provenance_unproven']
    assert not any(code.startswith('raw_results_changed_') for code in warnings)


def test_matching_dimensions_with_another_raw_fingerprint_prove_nothing(env):
    first = env.translate()
    rewrite_payload(env, first.artifact_id, lambda data: data.update(raw_fingerprint='sha256:' + '0' * 64))
    _, result, provider, warnings = continue_job(env, env.job)
    assert len(provider.requests) == 2 and warnings == ['raw_results_provenance_unproven']


@pytest.mark.parametrize('damage', [
    lambda path: path.write_bytes(path.read_bytes()[:-5]), lambda path: path.write_bytes(b'Z' + path.read_bytes()[1:]),
    lambda path: path.unlink(),
], ids=['partial', 'corrupt', 'missing'])
def test_a_damaged_earlier_artifact_is_redone_and_left_as_it_was(env, damage):
    first = env.translate()
    before = artifact_row(env, first.artifact_id)
    damage(env.final(first.artifact_id))
    _, result, provider, _ = continue_job(env, env.job)
    assert len(provider.requests) == 2 and {unit.source for unit in result.units} == {UnitSource.PROVIDER}
    assert artifact_row(env, first.artifact_id) == before


def test_a_longer_chain_reaches_the_job_that_stored_the_results(env):
    first = env.translate()
    middle, replayed, _, _ = continue_job(env, env.job)
    assert replayed.artifact_id == first.artifact_id
    _, result, provider, warnings = continue_job(env, middle, env.job)
    assert provider.requests == [] and warnings == []
    assert {unit.source for unit in result.units} == {UnitSource.ARTIFACT_REPLAY}


def test_the_nearest_record_decides_and_an_older_one_is_not_tried_after_a_change(env):
    env.translate()
    changed_plan = plan(chunk_limit=50)
    middle, _, _, _ = continue_job(env, env.job, subject_plan=changed_plan)
    # The middle job stored results under its own plan; the original plan now differs from that nearer record.
    _, result, provider, warnings = continue_job(env, middle, env.job)
    assert len(provider.requests) == 2 and warnings == ['raw_results_incompatible_plan', 'raw_results_changed_capability']


@pytest.mark.parametrize('reusable', [
    lambda env: (env.job.job_id, env.job.job_id), lambda env: ('not-a-job',), lambda env: [env.job.job_id],
    lambda env: tuple(str(__import__('uuid').uuid4()) for _ in range(MAX_REUSABLE_JOBS + 1)),
], ids=['duplicate', 'invalid', 'not-a-tuple', 'too-many'])
def test_an_invalid_chain_is_refused_before_any_work(env, reusable):
    job = env.running_job()
    provider = Provider()
    translator = DocumentTranslator(plan(), provider, upper_glossary, env.artifacts, env.provenance,
                                    InMemoryJobEventLog(job.job_id), clock=env.clock, new_id=env.new_id)
    with pytest.raises(TranslationExecutionError, match='^invalid_reusable_jobs$'):
        translator.translate(document(), ExecutionContext(job, job.lease.owner_token, reusable_jobs=reusable(env)))
    assert provider.requests == []


def test_a_job_cannot_list_itself_as_reusable(env):
    provider = Provider()
    with pytest.raises(TranslationExecutionError, match='^invalid_reusable_jobs$'):
        env.translate(provider=provider, context=env.context(reusable_jobs=(env.job.job_id,)))
    assert provider.requests == []


# ---------- Through the CLI, with the real project store and an instrumented transport.

from test_cli_rich_runtime import Transport, config, invoke, records, text  # noqa: E402
from test_cli_translate_project import _book, _jobs  # noqa: E402


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_processing_dir", lambda _: tmp_path / "processing")


def resume(tmp_path, monkeypatch, source):
    transport = Transport()
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: transport)
    return runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)]), transport


def test_resume_replays_finished_documents_even_without_the_v2_cache(tmp_path, monkeypatch, isolate):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    first, transport = invoke(tmp_path, monkeypatch, source, session=Transport(failing="Goodbye reader."))
    assert first.exit_code == 1 and transport.posts[-1] == "Goodbye reader.", text(first)
    parent = _jobs(tmp_path)[-1]
    # Without the cache only the raw results can spare these calls, so this proves the replay.
    composition.v2_cache_path(tmp_path / "cache.sqlite").unlink()
    resumed, transport = resume(tmp_path, monkeypatch, source)
    assert resumed.exit_code == 0, text(resumed)
    # Chapter one is replayed. Chapter two failed, so it runs again, including its head title that the cache once held.
    assert transport.posts == ["T", "Goodbye reader."]
    jobs = _jobs(tmp_path)
    child = next(job for job in jobs if job.continuation_of == parent.job_id)
    assert next(job for job in jobs if job.job_id == parent.job_id) == parent  # The ended job is not reopened.
    operations = [request.operation for request in records(tmp_path, child)]
    assert operations.count("replay_raw_results") >= 1
    assert "raw_results_" not in text(resumed)
    report = " ".join(text(resumed).split())
    # Replayed units count as reused, not as translated: only chapter two's two texts were translated.
    assert "Texts translated │ 2 " in report


def test_resume_after_a_model_change_names_the_dimension_and_translates_again(tmp_path, monkeypatch, isolate):
    config(tmp_path, "model-a")
    source = _book(tmp_path / "book.epub")
    first, _ = invoke(tmp_path, monkeypatch, source, session=Transport(failing="Goodbye reader."))
    assert first.exit_code == 1, text(first)
    composition.v2_cache_path(tmp_path / "cache.sqlite").unlink()
    config(tmp_path, "model-b")
    resumed, transport = resume(tmp_path, monkeypatch, source)
    assert resumed.exit_code == 0, text(resumed)
    assert "Goodbye reader." in transport.posts and "Hello reader." in transport.posts
    output = " ".join(text(resumed).split())
    assert "raw_results_changed_provider" in output


def test_a_second_resume_reaches_the_job_that_stored_the_results(tmp_path, monkeypatch, isolate):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    first, _ = invoke(tmp_path, monkeypatch, source, session=Transport(failing="Goodbye reader."))
    assert first.exit_code == 1, text(first)
    # The middle job replays chapter one, publishes nothing new for it, and fails on chapter two again.
    monkeypatch.setattr("ayvu.v1.translator.requests.Session", lambda: Transport(failing="Goodbye reader."))
    middle = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    assert middle.exit_code == 1, text(middle)
    composition.v2_cache_path(tmp_path / "cache.sqlite").unlink()
    resumed, transport = resume(tmp_path, monkeypatch, source)
    assert resumed.exit_code == 0, text(resumed)
    assert transport.posts == ["T", "Goodbye reader."]
    chain = [job for job in _jobs(tmp_path) if job.request.command_type == "translate_project"]
    assert len(chain) == 3 and chain[-1].continuation_of == chain[1].job_id and chain[1].continuation_of == chain[0].job_id
