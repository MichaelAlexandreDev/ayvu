from contextlib import closing
import sqlite3

import pytest

from ayvu.provenance.model import ActorKind, ProvenanceLimitError, ProvenanceNotFoundError, ProvenanceValidationError, SourceKind
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.provenance.store import MAX_LIST_LIMIT, MAX_TRAVERSAL_DEPTH, MAX_TRAVERSAL_RECORDS, SqliteProvenanceRepository
from tests.support.provenance_fakes import INPUT_FINGERPRINT, MACHINE, PROJECT, subject
from ayvu.kernel.sqlite import SqliteMigrationRunner

from tests.provenance.provenance_store_support import BIG, MISSING
from tests.provenance.provenance_store_support import chain, env, foreign_record
from tests.provenance.provenance_store_support import raw
from tests.support.paths import PROJECT_ROOT


def test_mixed_graph_traces_a_final_artifact_back_to_the_input(env):
    records = env.build_mixed_graph()
    reopened = env.open()
    trace = reopened.trace_artifact(env.artifact.artifact_id, **BIG)
    assert trace.producers == (records['publish'].record_id,) and not trace.graph.truncated
    found = {node.record.record_id: node.record for node in trace.graph.nodes}
    assert set(found) == {record.record_id for record in records.values()}
    assert {record.request.source_kind for record in found.values()} == {
        SourceKind.PIPELINE, SourceKind.CACHE, SourceKind.TRANSLATION_MEMORY, SourceKind.PROVIDER,
        SourceKind.HUMAN, SourceKind.VALIDATION}
    assert [(item.job_id, item.input_fingerprint) for item in trace.jobs] == [(env.job.job_id, INPUT_FINGERPRINT)]
    outcomes = {record.request.operation: record.request.outcome_code for record in found.values()}
    assert outcomes['human_review'] == 'approved' and outcomes['validate'] == 'passed'
    assert found[records['review'].record_id].request.actor_kind is ActorKind.HUMAN
    assert found[records['review'].record_id].request.subject == subject('unit-3')
    assert found[records['extract'].record_id].request.parents == ()


def test_traversal_is_deterministic_and_starts_from_the_requested_record(env):
    records = env.build_mixed_graph()
    first = env.repository.lineage(records['publish'].record_id, **BIG)
    second = env.open().lineage(records['publish'].record_id, **BIG)
    assert [node.record.record_id for node in first.nodes] == [node.record.record_id for node in second.nodes]
    assert first.nodes[0].record == records['publish']
    assert env.repository.lineage(records['extract'].record_id, **BIG).nodes[0].record == records['extract']
    assert len(env.repository.lineage(records['extract'].record_id, **BIG).nodes) == 1


def test_diamond_ancestry_is_not_mistaken_for_a_cycle(env):
    root = env.append('root')
    left, right = env.append('left', parents=(root.record_id,)), env.append('right', parents=(root.record_id,))
    join = env.append('join', parents=(left.record_id, right.record_id))
    graph = env.repository.lineage(join.record_id, **BIG)
    assert len(graph.nodes) == 4 and not graph.truncated


def test_depth_bound_is_exact_and_reported(env):
    tip = chain(env, 4)
    assert not env.repository.lineage(tip.record_id, max_depth=3, max_records=100).truncated
    graph = env.repository.lineage(tip.record_id, max_depth=2, max_records=100)
    assert graph.truncated and len(graph.nodes) == 3
    assert env.repository.lineage(tip.record_id, max_depth=1, max_records=100).truncated


def test_record_bound_is_exact_and_reported(env):
    tip = chain(env, 4)
    assert not env.repository.lineage(tip.record_id, max_depth=10, max_records=4).truncated
    graph = env.repository.lineage(tip.record_id, max_depth=10, max_records=3)
    assert graph.truncated and len(graph.nodes) == 3
    assert [node.record.record_id for node in graph.nodes] == [
        node.record.record_id for node in env.open().lineage(tip.record_id, max_depth=10, max_records=3).nodes]


@pytest.mark.parametrize('bounds', [
    dict(max_depth=None, max_records=10), dict(max_depth=10, max_records=None), dict(max_depth=0, max_records=10),
    dict(max_depth=10, max_records=0), dict(max_depth=True, max_records=10), dict(max_depth=10, max_records='10'),
    dict(max_depth=MAX_TRAVERSAL_DEPTH + 1, max_records=10), dict(max_depth=10, max_records=MAX_TRAVERSAL_RECORDS + 1),
], ids=str)
def test_unbounded_traversal_requests_fail(env, bounds):
    record = env.append()
    artifact = env.publish_artifact()
    with pytest.raises(ProvenanceLimitError, match='^invalid_traversal_limit$'):
        env.repository.lineage(record.record_id, **bounds)
    with pytest.raises(ProvenanceLimitError, match='^invalid_traversal_limit$'):
        env.repository.trace_artifact(artifact.artifact_id, **bounds)


def test_bounds_are_required_arguments(env):
    record = env.append()
    with pytest.raises(TypeError):
        env.repository.lineage(record.record_id)
    with pytest.raises(TypeError):
        env.repository.list_by_job(env.job.job_id)


@pytest.mark.parametrize('limit', [None, 0, -1, True, '1', MAX_LIST_LIMIT + 1])
def test_list_limits_are_validated(env, limit):
    for call in (lambda: env.repository.list_by_job(env.job.job_id, limit=limit),
                 lambda: env.repository.list_by_subject(subject('unit-1'), limit=limit),
                 lambda: env.repository.list_by_artifact(MISSING, limit=limit)):
        with pytest.raises(ProvenanceLimitError, match='^invalid_limit$'):
            call()


def test_queries_by_job_subject_and_artifact_are_ordered_and_report_truncation(env):
    records = env.build_mixed_graph()
    other_job = env.running_job()
    other = env.append('translate', job_id=other_job.job_id, subject=subject('unit-3'))
    by_job = env.repository.list_by_job(env.job.job_id, limit=MAX_LIST_LIMIT)
    assert [node.record for node in by_job.nodes] == list(records.values()) and not by_job.truncated
    first_two = env.repository.list_by_job(env.job.job_id, limit=2)
    assert len(first_two.nodes) == 2 and first_two.truncated
    assert [node.record for node in env.repository.list_by_job(env.job.job_id, limit=len(records)).nodes] == list(records.values())
    assert not env.repository.list_by_job(env.job.job_id, limit=len(records)).truncated
    by_subject = env.repository.list_by_subject(subject('unit-3'), limit=10)
    assert [node.record for node in by_subject.nodes] == [records['machine'], records['review'], other]
    assert env.repository.list_by_subject(subject('unit-9'), limit=10).nodes == ()
    by_artifact = env.repository.list_by_artifact(env.artifact.artifact_id, limit=10)
    assert [node.record for node in by_artifact.nodes] == [records['publish']]
    assert env.repository.list_by_artifact(MISSING, limit=10).nodes == ()


def test_subject_query_distinguishes_namespace_and_version(env):
    from ayvu.provenance.model import SubjectRef
    env.append(subject=SubjectRef('content_unit', 1, 'unit-1'))
    assert env.repository.list_by_subject(SubjectRef('content_unit', 2, 'unit-1'), limit=5).nodes == ()
    assert env.repository.list_by_subject(SubjectRef('other', 1, 'unit-1'), limit=5).nodes == ()
    with pytest.raises(ProvenanceValidationError, match='^invalid_subject$'):
        env.repository.list_by_subject('unit-1', limit=5)


def test_trace_of_an_artifact_without_records_is_empty_and_of_an_unknown_one_is_an_error(env):
    artifact = env.publish_artifact()
    trace = env.repository.trace_artifact(artifact.artifact_id, **BIG)
    assert trace.producers == () and trace.graph.nodes == () and trace.jobs == ()
    with pytest.raises(ProvenanceNotFoundError, match='^artifact_not_found$'):
        env.repository.trace_artifact(MISSING, **BIG)


def test_trace_reports_truncation_when_there_are_more_producers_than_the_bound(env):
    artifact = env.publish_artifact()
    for _ in range(3):
        env.append('publish', artifacts=(artifact.artifact_id,))
    trace = env.repository.trace_artifact(artifact.artifact_id, max_depth=5, max_records=2)
    assert trace.graph.truncated and len(trace.producers) == 2 and len(trace.graph.nodes) == 2


def test_trace_crosses_a_continuation_to_each_job_and_its_fingerprint(env):
    first = env.append('translate')
    other_job = env.running_job()
    artifact = env.publish_artifact()
    tip = env.append('publish', job_id=other_job.job_id, parents=(first.record_id,), artifacts=(artifact.artifact_id,))
    trace = env.repository.trace_artifact(artifact.artifact_id, **BIG)
    assert trace.producers == (tip.record_id,)
    assert {item.job_id for item in trace.jobs} == {env.job.job_id, other_job.job_id}


def test_explicit_unknowns_survive_persistence_and_are_not_identified(env):
    record = env.append('translate', producer=MACHINE)
    stored = env.open().get(record.record_id).record.request.producer
    assert stored.model == 'unknown' and stored.fully_identified is False


def test_review_style_identifier_round_trips_as_an_opaque_value(env):
    from ayvu.provenance.model import SubjectRef
    value = SubjectRef('legacy_review_position', 1, 'c0001-s0001')
    record = env.append(subject=value)
    assert env.open().get(record.record_id).record.request.subject == value
    assert [node.record for node in env.repository.list_by_subject(value, limit=5).nodes] == [record]
    assert env.repository.list_by_subject(SubjectRef('legacy_review_position', 1, 'c0001-s0002'), limit=5).nodes == ()


def test_provenance_modules_do_not_know_the_review_numbering_scheme():
    from pathlib import Path
    root = PROJECT_ROOT / 'src' / 'ayvu'
    for name in ('provenance/model.py', 'provenance/service.py', 'provenance/store.py'):
        text = (root / name).read_text()
        assert 'review_export' not in text and 'parse_segment_id' not in text and 'c####' not in text


def test_truncation_among_siblings_follows_a_fixed_order(env):
    parents = [env.append('parent') for _ in range(6)]
    tip = env.append('join', parents=tuple(parent.record_id for parent in parents))
    graph = env.repository.lineage(tip.record_id, max_depth=5, max_records=4)
    assert graph.truncated
    assert [node.record.record_id for node in graph.nodes[1:]] == sorted(parent.record_id for parent in parents)[:3]
    complete = env.repository.lineage(tip.record_id, max_depth=5, max_records=7)
    assert [node.record.record_id for node in complete.nodes[1:]] == sorted(parent.record_id for parent in parents)


def test_artifact_queries_exclude_a_linked_record_of_another_project(env):
    artifact = env.publish_artifact()
    mine = env.append('publish', artifacts=(artifact.artifact_id,))
    foreign = foreign_record(env)
    raw(env, 'INSERT INTO provenance_artifacts (record_id, artifact_id) VALUES (?, ?)', foreign.record_id, artifact.artifact_id)
    assert [node.record for node in env.repository.list_by_artifact(artifact.artifact_id, limit=10).nodes] == [mine]
    assert env.repository.trace_artifact(artifact.artifact_id, **BIG).producers == (mine.record_id,)


def test_traversal_reads_one_snapshot_that_a_writer_cannot_change_midway(env):
    tip = chain(env, 3)
    observed = []

    class Observing(SqliteProvenanceRepository):
        def _load(self, connection, record_id):
            record = super()._load(connection, record_id)
            with closing(sqlite3.connect(env.database, timeout=0, isolation_level=None)) as writer:
                try:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('CREATE TABLE attempt_to_change(value)')
                    writer.execute('COMMIT')
                    observed.append('changed')
                except sqlite3.OperationalError:
                    observed.append('protected')
            return record

    repository = Observing(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, PROJECT, clock=env.clock)
    repository.lineage(tip.record_id, **BIG)
    assert observed and set(observed) == {'protected'}
