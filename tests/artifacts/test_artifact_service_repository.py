import errno
from contextlib import closing
import sqlite3

import pytest

from ayvu.artifacts.model import ArtifactCancelledError, ArtifactIntegrityError, ArtifactOwnershipError, ArtifactPathError, ArtifactState, ArtifactStoreError, ArtifactValidationError
from ayvu.artifacts.service import ARTIFACT_SAFE_POINTS, ArtifactService
from ayvu.artifacts.store import LocalArtifactPayloads, SqliteArtifactRepository
from ayvu.jobs.model import JobState
from ayvu.jobs.execution import CancellationSource
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.kernel import JobCancellationError
from ayvu.kernel.sqlite import SqliteMigrationRunner

from tests.artifacts.artifact_service_support import DIGEST, FaultOps
from tests.artifacts.artifact_service_support import PAYLOAD, PROJECT, START
from tests.artifacts.artifact_service_support import SimulatedCrash, env, repository_of


@pytest.mark.parametrize('assignment', [
    "state = 'staging'", "digest = 'sha256:" + 'd' * 64 + "'", 'size = 1', "job_id = 'x'", "location = 'x'",
    "role = 'x'", "input_fingerprint = 'x'", 'schema_version = 2', 'revision = 1',
])
def test_database_itself_refuses_to_change_a_ready_record(env, assignment):
    env.publish()
    with closing(sqlite3.connect(env.database)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'UPDATE job_artifacts SET {assignment}')


def test_database_itself_refuses_to_change_an_abandoned_record(env):
    with pytest.raises(ArtifactStoreError):
        env.publish(env.service(FaultOps('write', OSError(errno.EIO, 'x'))))
    with closing(sqlite3.connect(env.database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match='immutable_artifact'):
            connection.execute("UPDATE job_artifacts SET state = 'ready', size = 1, digest = 'x', reason_code = NULL")


def test_database_refuses_two_active_records_and_an_unknown_job(env):
    env.publish()
    columns = ('artifact_id, schema_version, project_id, job_id, role, stage, stage_version, input_fingerprint, '
               'media_type, location, state, revision, size, digest, reason_code, created_at, updated_at')
    copy = columns.replace('artifact_id, ', "'new', ", 1).replace('location', "'other'", 1)
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'INSERT INTO job_artifacts ({columns}) SELECT {copy} FROM job_artifacts')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE translation_jobs SET job_id = 'other'")
        unknown = copy.replace('job_id', "'unknown-job'", 1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'INSERT INTO job_artifacts ({columns}) SELECT {unknown} FROM job_artifacts')


@pytest.mark.parametrize('moment', ['artifact_before_write', 'artifact_between_chunks', 'artifact_before_publication'])
def test_cancellation_at_each_safe_point_leaves_no_ready_record(env, moment):
    source = CancellationSource(ARTIFACT_SAFE_POINTS)

    def chunks():
        if moment == 'artifact_between_chunks':
            source.request('user_requested', START)
        yield b'one'
        yield b'two'
        if moment == 'artifact_before_publication':
            source.request('user_requested', START)

    if moment == 'artifact_before_write':
        source.request('user_requested', START)
    with pytest.raises(ArtifactCancelledError, match='^publication_cancelled$'):
        env.service().publish(env.request(), chunks(), owner_token=env.token, cancellation=source.token)
    assert source.acknowledgement.safe_point == moment
    rows = env.rows()
    assert all(row[1] != 'ready' for row in rows)
    if moment == 'artifact_before_write':
        assert rows == [] and env.files() == []
    else:
        assert rows[0][1:3] == ('abandoned', 'cancelled')
        assert not env.final_path(rows[0][0]).exists()
    assert env.service().find_reusable(env.request()).artifact is None


def test_cancellation_after_publication_does_not_retract_a_ready_artifact(env):
    source = CancellationSource(ARTIFACT_SAFE_POINTS)
    artifact = env.service().publish(env.request(), PAYLOAD, owner_token=env.token, cancellation=source.token)
    source.request('user_requested', START)
    assert artifact.state is ArtifactState.READY and env.rows()[0][1] == 'ready'


def test_cancellation_source_must_declare_the_artifact_safe_points(env):
    source = CancellationSource(('another_point',))
    with pytest.raises(JobCancellationError, match='^undeclared_safe_point$'):
        env.service().publish(env.request(), PAYLOAD, owner_token=env.token, cancellation=source.token)
    assert env.rows() == []


def test_expired_owner_failure_leaves_staging_for_reconciliation(env):
    def chunks():
        yield b'data'
        env.expire_lease()
        raise RuntimeError('source failed')

    with pytest.raises(ArtifactStoreError, match='^payload_source_failed$'):
        env.publish(chunks=chunks())
    assert env.rows()[0][1] == 'staging'
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'interrupted_before_digest')


def test_busy_store_is_a_typed_error_without_a_ready_record(env):
    runner = SqliteMigrationRunner(PROJECT_MIGRATIONS, busy_timeout=0.01)
    repository = SqliteArtifactRepository(runner, env.database, PROJECT, clock=env.clock, new_id=env.new_id)
    service = ArtifactService(repository, LocalArtifactPayloads(env.root))
    with closing(sqlite3.connect(env.database, isolation_level=None)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(ArtifactStoreError, match='^busy$'):
            env.publish(service)
        blocker.execute('ROLLBACK')
    assert env.rows() == []


def test_repository_rejects_invalid_arguments(env):
    repository = repository_of(env)
    for call in (lambda: repository.reserve('x', env.token), lambda: repository.find_ready('x'),
                 lambda: repository.list_staging(0), lambda: repository.list_staging(True),
                 lambda: repository.get('../x')):
        with pytest.raises(ArtifactValidationError):
            call()


def test_repr_shows_no_paths_tokens_or_digests(env):
    artifact = env.publish()
    repository = repository_of(env)
    text = ' '.join(map(repr, (artifact, repository, env.service(), LocalArtifactPayloads(env.root))))
    assert env.token not in text and DIGEST not in text and str(env.tmp_path) not in text


def test_errors_never_contain_paths(env):
    outside = env.tmp_path / 'outside'
    outside.mkdir()
    env.root.mkdir()
    (env.root / 'ready').symlink_to(outside)
    with pytest.raises(ArtifactPathError) as caught:
        env.publish()
    assert str(env.tmp_path) not in str(caught.value) and str(env.tmp_path) not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_original_project_files_are_never_touched(env):
    bystander = env.tmp_path / 'book.epub'
    bystander.write_bytes(b'original')
    resume = env.tmp_path / 'book.ayvu-state.json'
    resume.write_bytes(b'{}')
    env.publish()
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('link')), stage_version=2)
    env.expire_lease()
    env.service().reconcile()
    assert bystander.read_bytes() == b'original' and resume.read_bytes() == b'{}'


def test_publisher_delayed_past_its_lease_never_becomes_ready(env):
    ops = FaultOps()

    def lose_lease_just_before_linking(name):
        if name == 'link':
            ops.observer = None
            env.expire_lease()
            env.service().reconcile()

    ops.observer = lose_lease_just_before_linking
    with pytest.raises(ArtifactOwnershipError, match='^lease_expired$'):
        env.publish(env.service(ops))
    artifact_id = env.rows()[0][0]
    assert env.rows()[0][1:3] == ('abandoned', 'interrupted_before_publication')
    # The late file exists, but no record advertises it.
    assert env.final_path(artifact_id).exists()
    assert env.service().find_reusable(env.request()).artifact is None


def test_terminal_job_without_a_lease_is_reconciled_immediately(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('write')))
    env.jobs.transition(env.job.job_id, JobState.FAILED, expected_revision=env.job.revision)
    report = env.service().reconcile()
    assert len(report.abandoned) == 1 and report.active == ()


def test_new_directories_are_synced_with_their_parent(env):
    ops = FaultOps()
    env.publish(env.service(ops))
    synced = [name.split(':')[1] for name in ops.calls if name.startswith('sync_directory:')]
    assert env.tmp_path.name in synced and synced.count('artifacts') == 2
    ops = FaultOps()
    env.publish(env.service(ops), stage_version=2)
    assert not {env.tmp_path.name, 'artifacts'} & {name.split(':')[1] for name in ops.calls if name.startswith('sync_directory:')}


def test_reconciliation_does_not_disturb_a_publication_between_link_and_unlink(env):
    ops = FaultOps()
    seen = {}

    def reconcile_in_the_window(name):
        if name == 'unlink':
            ops.observer = None
            seen['report'] = env.service().reconcile()

    ops.observer = reconcile_in_the_window
    artifact = env.publish(env.service(ops))
    assert artifact.state is ArtifactState.READY
    assert seen['report'].active == (artifact.artifact_id,)


def test_final_file_is_verified_after_linking_before_it_can_become_ready(env):
    class WrongTarget(FaultOps):
        def link(self, source, target):
            target.write_bytes(b'not what was staged')

    with pytest.raises(ArtifactIntegrityError, match='^final_payload_mismatch$'):
        env.publish(env.service(WrongTarget()))
    artifact_id = env.rows()[0][0]
    assert env.rows()[0][1] == 'staging' and env.staged_path(artifact_id).exists()
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'final_mismatch')


def test_reconciliation_keeps_a_staging_file_that_is_a_separate_copy(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('sync_directory:ready')))
    artifact_id = env.rows()[0][0]
    # Same bytes under another inode: not the interrupted link, so not ours to remove.
    staged = env.staged_path(artifact_id)
    staged.unlink()
    staged.write_bytes(b''.join(PAYLOAD))
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1] == 'ready' and staged.exists()
