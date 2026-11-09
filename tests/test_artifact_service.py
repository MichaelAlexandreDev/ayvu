import errno
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3
import stat
from threading import Barrier, Event
from uuid import UUID

import pytest

from ayvu.artifact import (
    Artifact, ArtifactCancelledError, ArtifactConflictError, ArtifactIntegrityError, ArtifactNotFoundError,
    ArtifactOwnershipError, ArtifactPathError, ArtifactRequest, ArtifactState, ArtifactStoreError,
    ArtifactValidationError,
)
from ayvu.artifact_service import ARTIFACT_SAFE_POINTS, ArtifactService
from ayvu.artifact_store import ArtifactFileOps, LocalArtifactPayloads, SqliteArtifactRepository
from ayvu.job import JobRequest, JobState
from ayvu.job_execution import CancellationSource, JobCancellationError
from ayvu.project_schema import PROJECT_MIGRATIONS, open_artifact_service, open_job_repository
from ayvu.sqlite_migrations import SqliteMigrationRunner

PROJECT = str(UUID(int=1))
OTHER_PROJECT = str(UUID(int=99))
START = datetime(2026, 11, 3, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
PAYLOAD = (b'first chunk ', b'second chunk')
DIGEST = 'sha256:' + hashlib.sha256(b''.join(PAYLOAD)).hexdigest()
SIZE = len(b''.join(PAYLOAD))


class SimulatedCrash(BaseException):
    """Not an Exception: no recovery code may run, exactly as in a real process death."""


class FaultOps(ArtifactFileOps):
    """Fails at the named step, either before it takes effect or just after."""

    def __init__(self, step=None, error=SimulatedCrash, *, after=False, occurrence=1):
        self.step, self.error, self.after, self.remaining = step, error, after, occurrence
        self.calls = []
        self.observer = None

    def _run(self, name, action):
        self.calls.append(name)
        if self.observer is not None:
            self.observer(name)
        hit = name == self.step
        if hit:
            self.remaining -= 1
            hit = self.remaining == 0
        if hit and not self.after:
            raise self.error
        result = action()
        if hit:
            raise self.error
        return result

    def open_new(self, path):
        return self._run('open_new', lambda: super(FaultOps, self).open_new(path))

    def write(self, stream, data):
        return self._run('write', lambda: super(FaultOps, self).write(stream, data))

    def sync(self, stream):
        return self._run('sync', lambda: super(FaultOps, self).sync(stream))

    def link(self, source, target):
        return self._run('link', lambda: super(FaultOps, self).link(source, target))

    def unlink(self, path):
        return self._run('unlink', lambda: super(FaultOps, self).unlink(path))

    def sync_directory(self, path):
        return self._run(f'sync_directory:{path.name}', lambda: super(FaultOps, self).sync_directory(path))


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.database = tmp_path / 'project.sqlite'
        self.backups = tmp_path / 'backups'
        self.root = tmp_path / 'artifacts'
        self.now = START
        self.next_id = 500
        self.jobs = open_job_repository(self.database, self.backups, PROJECT, clock=self.clock)
        self.job = self.running_job()
        self.token = self.jobs.acquire_lease(self.job.job_id, MINUTE).lease.owner_token

    def clock(self):
        return self.now

    def new_id(self):
        self.next_id += 1
        return str(UUID(int=self.next_id))

    def running_job(self):
        job = self.jobs.create(JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'f' * 64))
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = self.jobs.transition(job.job_id, state, expected_revision=job.revision)
        return job

    def service(self, ops=None):
        return open_artifact_service(self.database, self.backups, self.root, PROJECT, ops=ops, clock=self.clock,
                                     new_id=self.new_id)

    def request(self, **changes):
        return ArtifactRequest(**{'project_id': PROJECT, 'job_id': self.job.job_id, 'role': 'translated_document',
                                  'stage': 'translate', 'stage_version': 1, 'input_fingerprint': 'sha256:' + 'a' * 64,
                                  'media_type': 'application/epub+zip', **changes})

    def publish(self, service=None, chunks=PAYLOAD, **changes):
        return (service or self.service()).publish(self.request(**changes), chunks, owner_token=self.token)

    def expire_lease(self):
        self.now += MINUTE

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                'SELECT artifact_id, state, reason_code, size, digest FROM job_artifacts ORDER BY created_at, artifact_id'
            ).fetchall()

    def staged_path(self, artifact_id):
        return self.root / 'staging' / f'{artifact_id}.part'

    def final_path(self, artifact_id):
        return self.root / 'ready' / f'{artifact_id}.bin'

    def files(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*') if path.is_file())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_publication_ends_ready_with_a_verified_file_and_no_staging_name(env):
    artifact = env.publish()
    assert (artifact.state, artifact.size, artifact.digest, artifact.revision) == (ArtifactState.READY, SIZE, DIGEST, 3)
    assert env.final_path(artifact.artifact_id).read_bytes() == b''.join(PAYLOAD)
    assert env.files() == [f'ready/{artifact.artifact_id}.bin']
    assert env.final_path(artifact.artifact_id).stat().st_nlink == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX modes')
def test_files_and_directories_are_private(env):
    artifact = env.publish()
    assert stat.S_IMODE(env.final_path(artifact.artifact_id).stat().st_mode) == 0o600
    assert stat.S_IMODE((env.root / 'ready').stat().st_mode) == 0o700


def test_empty_payload_is_a_valid_artifact(env):
    artifact = env.publish(chunks=())
    assert artifact.size == 0 and env.service().find_reusable(env.request()).artifact == artifact


def test_ready_artifact_survives_restart_and_is_reused(env):
    published = env.publish()
    result = env.service().find_reusable(env.request())
    assert result.artifact == published and result.reason is None
    with env.service().open_reusable(published.artifact_id, env.request()) as stream:
        assert stream.read() == b''.join(PAYLOAD)


def test_no_transaction_is_open_during_any_payload_operation(env):
    ops = FaultOps()
    observed = []

    def try_lock(name):
        with closing(sqlite3.connect(env.database, timeout=0, isolation_level=None)) as other:
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute('ROLLBACK')
                observed.append((name, 'free'))
            except sqlite3.OperationalError:
                observed.append((name, 'locked'))

    ops.observer = try_lock
    def chunks():
        try_lock('chunk_source')
        yield b'data'
    env.publish(env.service(ops), chunks=chunks())
    names = {name for name, _ in observed}
    assert {'chunk_source', 'open_new', 'write', 'sync', 'link', 'unlink'} <= names
    assert {state for _, state in observed} == {'free'}


def test_nothing_is_ready_before_the_final_file_exists(env):
    ops = FaultOps()
    states = []
    ops.observer = lambda name: states.append(env.rows()[-1][1])
    env.publish(env.service(ops))
    assert set(states) == {'staging'} and env.rows()[-1][1] == 'ready'


CRASH_POINTS = [
    # step, after, expected state, reason, staged file retained, final file exists
    ('open_new', False, 'abandoned', 'interrupted_before_digest', False, False),
    ('write', False, 'abandoned', 'interrupted_before_digest', True, False),
    ('sync', False, 'abandoned', 'interrupted_before_digest', True, False),
    ('sync_directory:staging', False, 'abandoned', 'interrupted_before_digest', True, False),
    ('link', False, 'abandoned', 'interrupted_before_publication', True, False),
    ('link', True, 'ready', None, False, True),
    ('sync_directory:ready', False, 'ready', None, False, True),
    ('unlink', False, 'ready', None, False, True),
    ('unlink', True, 'ready', None, False, True),
]


@pytest.mark.parametrize('step,after,state,reason,staged,final', CRASH_POINTS)
def test_every_crash_boundary_reconciles_without_advertising_a_partial_file(env, step, after, state, reason, staged, final):
    ops = FaultOps(step, after=after)
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(ops))
    artifact_id = env.rows()[0][0]
    assert env.rows()[0][1] == 'staging'
    assert env.service().find_reusable(env.request()).artifact is None
    env.expire_lease()

    report = env.service().reconcile()

    assert (artifact_id in report.completed) == (state == 'ready')
    assert (artifact_id in report.abandoned) == (state == 'abandoned')
    assert env.rows()[0][:3] == (artifact_id, state, reason)
    assert env.staged_path(artifact_id).exists() == staged
    assert env.final_path(artifact_id).exists() == final
    # A second pass finds nothing left to do, and the outcome is stable.
    second = env.service().reconcile()
    assert (second.completed, second.abandoned, second.active) == ((), (), ())
    assert env.rows()[0][:3] == (artifact_id, state, reason)


@pytest.mark.parametrize('step,after,state,reason,staged,final', CRASH_POINTS)
def test_reuse_after_reconciliation_agrees_with_the_verified_state(env, step, after, state, reason, staged, final):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps(step, after=after)))
    env.expire_lease()
    env.service().reconcile()
    # Reuse is read-only and needs no lease.
    result = env.service().find_reusable(env.request())
    assert (result.artifact is not None) == (state == 'ready')
    if state == 'ready':
        assert result.artifact.digest == DIGEST


class CrashingRepository:
    def __init__(self, inner, method, error=SimulatedCrash):
        self._inner, self._method, self._error = inner, method, error

    def __getattr__(self, name):
        if name == self._method:
            def fail(*_, **__):
                raise self._error
            return fail
        return getattr(self._inner, name)


def repository_of(env):
    return SqliteArtifactRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS), env.database, PROJECT, clock=env.clock,
                                    new_id=env.new_id)


def test_crash_before_recording_the_digest_abandons_and_retains_the_temporary_file(env):
    service = ArtifactService(CrashingRepository(repository_of(env), 'record_payload'), LocalArtifactPayloads(env.root))
    with pytest.raises(SimulatedCrash):
        env.publish(service)
    artifact_id = env.rows()[0][0]
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'interrupted_before_digest')
    assert env.staged_path(artifact_id).read_bytes() == b''.join(PAYLOAD)


def test_crash_before_the_ready_commit_completes_on_reconciliation(env):
    service = ArtifactService(CrashingRepository(repository_of(env), 'mark_ready'), LocalArtifactPayloads(env.root))
    with pytest.raises(SimulatedCrash):
        env.publish(service)
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1] == 'ready'
    assert env.service().find_reusable(env.request()).reason is None


def test_ready_commit_failure_stays_staged_and_is_reported(env):
    service = ArtifactService(CrashingRepository(repository_of(env), 'mark_ready', ArtifactStoreError('busy')),
                              LocalArtifactPayloads(env.root))
    with pytest.raises(ArtifactStoreError, match='^busy$'):
        env.publish(service)
    assert env.rows()[0][1] == 'staging'


@pytest.mark.parametrize('step,error,code', [
    ('open_new', OSError(errno.ENOSPC, 'x'), 'disk_full'),
    ('write', OSError(errno.ENOSPC, 'x'), 'disk_full'),
    ('write', OSError(errno.EIO, 'x'), 'io'),
    ('sync', OSError(errno.EIO, 'x'), 'io'),
    ('sync', PermissionError(errno.EACCES, 'x'), 'permission'),
])
def test_injected_write_failures_abandon_with_the_typed_reason(env, step, error, code):
    with pytest.raises(ArtifactStoreError, match=f'^{code}$'):
        env.publish(env.service(FaultOps(step, error)))
    assert env.rows()[0][1:3] == ('abandoned', code)
    assert not (env.root / 'ready').exists() or not list((env.root / 'ready').iterdir())


@pytest.mark.parametrize('step,after,final_state', [
    ('link', False, 'abandoned'), ('sync_directory:ready', False, 'ready'), ('unlink', False, 'ready'),
])
def test_injected_publication_failures_stay_staged_until_reconciled(env, step, after, final_state):
    with pytest.raises(ArtifactStoreError, match='^io$'):
        env.publish(env.service(FaultOps(step, OSError(errno.EIO, 'x'), after=after)))
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1] == final_state


def test_write_that_does_not_reach_the_disk_is_detected_before_publication(env):
    class Truncating(FaultOps):
        def write(self, stream, data):
            return super().write(stream, data[:-1])

    with pytest.raises(ArtifactIntegrityError, match='^staged_payload_mismatch$'):
        env.publish(env.service(Truncating()))
    assert env.rows()[0][1:3] == ('abandoned', 'staged_payload_mismatch')
    assert not (env.root / 'ready').exists() or not list((env.root / 'ready').iterdir())


def test_chunk_source_exception_is_normalized_without_its_message(env):
    canary = 'Call me Ishmael.'

    def chunks():
        yield b'partial'
        raise RuntimeError(canary)

    with pytest.raises(ArtifactStoreError, match='^payload_source_failed$') as caught:
        env.publish(chunks=chunks())
    assert caught.value.__cause__ is None and canary not in repr(caught.value)
    assert env.rows()[0][1:3] == ('abandoned', 'payload_source_failed')


@pytest.mark.parametrize('chunks', [['text'], [bytearray(b'x')], [None], [memoryview(b'x')]])
def test_only_bytes_are_accepted_as_chunks(env, chunks):
    with pytest.raises(ArtifactValidationError, match='^invalid_chunk$'):
        env.publish(chunks=chunks)
    assert env.rows()[0][1:3] == ('abandoned', 'invalid_chunk')


def test_size_limit_stops_the_write(env, monkeypatch):
    monkeypatch.setattr('ayvu.artifact_store.MAX_ARTIFACT_BYTES', 4)
    consumed = []

    def chunks():
        for chunk in (b'123', b'456', b'789'):
            consumed.append(chunk)
            yield chunk

    with pytest.raises(ArtifactValidationError, match='^artifact_too_large$'):
        env.publish(chunks=chunks())
    assert len(consumed) == 2 and env.rows()[0][1:3] == ('abandoned', 'artifact_too_large')


def test_publication_requires_the_live_lease_of_the_owning_job(env):
    before = env.rows()
    with pytest.raises(ArtifactOwnershipError, match='^lease_not_held$'):
        env.service().publish(env.request(), PAYLOAD, owner_token='0' * 64)
    env.expire_lease()
    with pytest.raises(ArtifactOwnershipError, match='^lease_expired$'):
        env.publish()
    assert env.rows() == before and env.files() == []


def test_terminal_job_cannot_own_a_new_artifact(env):
    env.jobs.transition(env.job.job_id, JobState.FAILED, expected_revision=env.job.revision)
    with pytest.raises(ArtifactOwnershipError, match='^lease_not_held$'):
        env.publish()


def test_unknown_job_and_foreign_project_are_refused_without_a_record(env):
    with pytest.raises(ArtifactNotFoundError, match='^job_not_found$'):
        env.publish(job_id=str(UUID(int=77)))
    with pytest.raises(ArtifactOwnershipError, match='^project_mismatch$'):
        env.publish(project_id=OTHER_PROJECT)
    assert env.rows() == []


def test_job_of_another_project_in_the_database_is_refused(env):
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER translation_jobs_immutable')
        connection.execute('UPDATE translation_jobs SET project_id = ? WHERE job_id = ?', (OTHER_PROJECT, env.job.job_id))
        connection.commit()
    with pytest.raises(ArtifactOwnershipError, match='^ownership_mismatch$'):
        env.publish()


def test_lease_lost_during_the_write_prevents_ready_and_reconciliation_resolves_it(env):
    def chunks():
        yield b'data'
        env.expire_lease()

    with pytest.raises(ArtifactOwnershipError, match='^lease_expired$'):
        env.publish(chunks=chunks())
    assert env.rows()[0][1] == 'staging' and env.rows()[0][3] is None
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'interrupted_before_digest')


def test_reconciliation_does_not_touch_a_job_with_a_live_lease(env):
    seen = {}

    def chunks():
        yield b'data'
        seen['report'] = env.service().reconcile()

    artifact = env.publish(chunks=chunks())
    report = seen['report']
    assert report.active == (artifact.artifact_id,) and report.abandoned == report.completed == ()
    assert artifact.state is ArtifactState.READY


def test_settling_refuses_a_job_that_acquired_a_lease_after_the_listing(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('write')))
    env.expire_lease()
    repository = repository_of(env)
    original = repository.list_staging

    def list_then_lose_race(limit):
        listed = original(limit)
        with closing(sqlite3.connect(env.database)) as connection:
            connection.execute('UPDATE translation_jobs SET lease_expires_at = ? WHERE job_id = ?',
                               ((env.now + MINUTE).isoformat(timespec='microseconds'), env.job.job_id))
            connection.commit()
        return listed

    repository.list_staging = list_then_lose_race
    report = ArtifactService(repository, LocalArtifactPayloads(env.root)).reconcile()
    assert len(report.active) == 1 and report.abandoned == ()
    assert env.rows()[0][1] == 'staging'


def test_reconciliation_batch_reports_when_more_remain(env):
    for number in range(3):
        env.now += timedelta(seconds=1)
        with pytest.raises(SimulatedCrash):
            env.publish(env.service(FaultOps('write')), stage_version=number + 1)
    env.expire_lease()
    first = env.service().reconcile(limit=2)
    assert len(first.abandoned) == 2 and first.more
    second = env.service().reconcile(limit=2)
    assert len(second.abandoned) == 1 and not second.more


def test_reconciliation_never_deletes_a_payload(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('sync_directory:staging')))
    artifact_id = env.rows()[0][0]
    env.expire_lease()
    before = env.staged_path(artifact_id).read_bytes()
    env.service().reconcile()
    assert env.staged_path(artifact_id).read_bytes() == before


def test_second_publisher_conflicts_while_the_first_is_in_progress_and_after_it_is_ready(env):
    seen = {}

    def chunks():
        yield b'data'
        with pytest.raises(ArtifactConflictError, match='^artifact_in_progress$') as caught:
            env.publish()
        seen['error'] = caught.value

    env.publish(chunks=chunks())
    assert 'error' in seen and len(env.rows()) == 1
    with pytest.raises(ArtifactConflictError, match='^artifact_exists$'):
        env.publish()
    assert len(env.rows()) == 1


def test_racing_publishers_have_exactly_one_winner(env):
    barrier = Barrier(2)
    both_reserved = Event()

    def publish(index):
        barrier.wait(timeout=5)
        try:
            return env.publish(chunks=(bytes([index]),))
        except ArtifactConflictError as error:
            both_reserved.set()
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(publish, range(2)))
    ready = [result for result in results if isinstance(result, Artifact)]
    losers = [result for result in results if not isinstance(result, Artifact)]
    assert len(ready) == 1 and both_reserved.is_set()
    assert losers[0] in {'artifact_in_progress', 'artifact_exists'}
    assert [row[1] for row in env.rows()] == ['ready']


def test_abandoned_attempt_allows_a_new_publication_with_a_new_identity(env):
    with pytest.raises(ArtifactStoreError):
        env.publish(env.service(FaultOps('write', OSError(errno.EIO, 'x'))))
    old = env.rows()[0][0]
    retry = env.publish()
    assert retry.artifact_id != old and env.staged_path(old).exists()
    assert env.service().find_reusable(env.request()).artifact == retry


@pytest.mark.parametrize('changes', [
    {'role': 'report'}, {'stage': 'validate'}, {'stage_version': 2}, {'input_fingerprint': 'sha256:' + 'c' * 64},
    {'job_id': str(UUID(int=77))},
])
def test_different_identity_finds_nothing_to_reuse(env, changes):
    env.publish()
    result = env.service().find_reusable(env.request(**changes))
    assert result.artifact is None and result.reason == 'artifact_not_found'


@pytest.mark.parametrize('changes,reason', [
    ({'role': 'report'}, 'role_mismatch'), ({'media_type': 'text/plain'}, 'role_mismatch'),
    ({'stage': 'validate'}, 'stage_mismatch'), ({'stage_version': 2}, 'stage_mismatch'),
    ({'input_fingerprint': 'sha256:' + 'c' * 64}, 'fingerprint_mismatch'),
    ({'job_id': str(UUID(int=77))}, 'ownership_mismatch'), ({'project_id': OTHER_PROJECT}, 'ownership_mismatch'),
])
def test_opening_by_identifier_refuses_any_mismatch(env, changes, reason):
    artifact = env.publish()
    with pytest.raises(ArtifactConflictError, match=f'^{reason}$'):
        env.service().open_reusable(artifact.artifact_id, env.request(**changes))


def test_media_type_mismatch_in_lookup_is_refused(env):
    env.publish()
    result = env.service().find_reusable(env.request(media_type='text/plain'))
    assert result.artifact is None and result.reason == 'role_mismatch'


def test_unknown_artifact_and_invalid_identity_are_typed(env):
    with pytest.raises(ArtifactNotFoundError, match='^artifact_not_found$'):
        env.service().open_reusable(str(UUID(int=77)), env.request())
    with pytest.raises(ArtifactValidationError, match='^invalid_identity$'):
        env.service().open_reusable('../../etc/passwd', env.request())


@pytest.mark.parametrize('corrupt,reason', [
    (lambda path: path.write_bytes(b'x'), 'payload_mismatch'),
    (lambda path: path.write_bytes(b'Z' + b''.join(PAYLOAD)[1:]), 'payload_mismatch'),
    (lambda path: path.unlink(), 'payload_missing'),
])
def test_existence_alone_never_proves_validity(env, corrupt, reason):
    artifact = env.publish()
    corrupt(env.final_path(artifact.artifact_id))
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == reason
    with pytest.raises(ArtifactIntegrityError, match=f'^{reason}$'):
        env.service().open_reusable(artifact.artifact_id, env.request())
    assert env.rows()[0][1] == 'ready'


def test_corrupt_artifact_is_retired_only_on_request_and_work_can_be_redone(env):
    artifact = env.publish()
    path = env.final_path(artifact.artifact_id)
    path.write_bytes(b'corrupted')
    assert env.rows()[0][1] == 'ready'
    with pytest.raises(ArtifactConflictError, match='^artifact_exists$'):
        env.publish()
    retired = env.service().retire_if_corrupt(artifact.artifact_id)
    assert (retired.state, retired.reason_code) == (ArtifactState.ABANDONED, 'integrity_failed')
    assert path.read_bytes() == b'corrupted'
    redone = env.publish()
    assert redone.artifact_id != artifact.artifact_id
    assert env.service().find_reusable(env.request()).artifact == redone


def test_retire_leaves_valid_and_non_ready_records_alone(env):
    artifact = env.publish()
    assert env.service().retire_if_corrupt(artifact.artifact_id) == artifact
    with pytest.raises(ArtifactStoreError):
        env.publish(env.service(FaultOps('write', OSError(errno.EIO, 'x'))), stage_version=2)
    abandoned_id = env.rows()[1][0] if env.rows()[1][1] == 'abandoned' else env.rows()[0][0]
    assert env.service().retire_if_corrupt(abandoned_id).state is ArtifactState.ABANDONED


def test_ready_directory_symlink_is_refused_without_writing_outside(env):
    outside = env.tmp_path / 'outside'
    outside.mkdir()
    env.root.mkdir()
    (env.root / 'ready').symlink_to(outside)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert list(outside.iterdir()) == []
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'unsafe_location')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('link_name', ['artifacts', 'artifacts/staging'])
def test_root_and_staging_symlinks_are_refused_without_writing_outside(env, link_name):
    outside = env.tmp_path / 'outside'
    outside.mkdir()
    link = env.tmp_path / link_name
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert list(outside.iterdir()) == []
    assert env.rows()[0][1:3] == ('abandoned', 'symlink_path')


def test_staging_path_that_is_already_a_symlink_is_not_followed(env):
    env.next_id = 700
    victim = env.tmp_path / 'victim'
    victim.write_bytes(b'unrelated')
    (env.root / 'staging').mkdir(parents=True)
    env.staged_path(str(UUID(int=701))).symlink_to(victim)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert victim.read_bytes() == b'unrelated'


def test_existing_file_at_the_staging_path_is_not_replaced(env):
    env.next_id = 700
    (env.root / 'staging').mkdir(parents=True)
    existing = env.staged_path(str(UUID(int=701)))
    existing.write_bytes(b'unrelated')
    with pytest.raises(ArtifactConflictError, match='^staging_path_exists$'):
        env.publish()
    assert existing.read_bytes() == b'unrelated'


def test_ready_path_collision_never_overwrites_and_reconciles_as_mismatch(env):
    env.next_id = 700
    (env.root / 'ready').mkdir(parents=True)
    existing = env.final_path(str(UUID(int=701)))
    existing.write_bytes(b'unrelated')
    with pytest.raises(ArtifactConflictError, match='^ready_path_collision$'):
        env.publish()
    assert existing.read_bytes() == b'unrelated'
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'final_mismatch')
    assert existing.read_bytes() == b'unrelated'
    assert env.staged_path(str(UUID(int=701))).read_bytes() == b''.join(PAYLOAD)


def test_symlink_swapped_in_after_publication_is_not_followed(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    outside = env.tmp_path / 'outside.bin'
    outside.write_bytes(b''.join(PAYLOAD))
    final.unlink()
    final.symlink_to(outside)
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'symlink_path'
    assert env.service().retire_if_corrupt(artifact.artifact_id).state is ArtifactState.ABANDONED
    assert outside.read_bytes() == b''.join(PAYLOAD)


def test_hardlinked_final_file_is_refused_even_with_matching_content(env):
    artifact = env.publish()
    os.link(env.final_path(artifact.artifact_id), env.tmp_path / 'alias.bin')
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'payload_has_aliases'
    assert env.service().retire_if_corrupt(artifact.artifact_id).state is ArtifactState.ABANDONED


def test_foreign_hardlink_is_not_taken_for_an_interrupted_publication(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('sync_directory:ready')))
    artifact_id = env.rows()[0][0]
    # The staged name no longer refers to the final file: its bytes are a separate copy.
    staged = env.staged_path(artifact_id)
    staged.unlink()
    staged.write_bytes(b''.join(PAYLOAD))
    os.link(env.final_path(artifact_id), env.tmp_path / 'alias.bin')
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'final_mismatch')
    assert staged.exists()


def test_directory_at_the_final_path_is_refused(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    final.unlink()
    final.mkdir()
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'not_a_regular_file'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO')
def test_fifo_at_the_final_path_does_not_block(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    final.unlink()
    os.mkfifo(final)
    assert env.service().find_reusable(env.request()).reason == 'not_a_regular_file'


def _tamper(env, assignment):
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute(f'UPDATE job_artifacts SET {assignment}')
        connection.commit()


@pytest.mark.parametrize('location', ['../../../etc/passwd', '/etc/passwd', 'artifacts/ready/other.bin', '', 'artifacts/ready/../../project.sqlite'])
def test_tampered_stored_location_is_rejected_without_touching_the_path(env, location):
    artifact = env.publish()
    _tamper(env, f"location = '{location}'")
    with pytest.raises(ArtifactPathError, match='^invalid_location$'):
        env.service().find_reusable(env.request())
    with pytest.raises(ArtifactPathError, match='^invalid_location$'):
        env.service().open_reusable(artifact.artifact_id, env.request())


@pytest.mark.parametrize('assignment,error,kind', [
    ('schema_version = 2', 'unsupported_artifact_version', ArtifactValidationError),
    ("state = 'unknown'", 'invalid_artifact_record', ArtifactStoreError),
    ("created_at = 'x'", 'invalid_artifact_record', ArtifactStoreError),
    ("input_fingerprint = 'private canary'", 'invalid_artifact_record', ArtifactStoreError),
])
def test_invalid_rows_fail_closed_without_exposing_stored_data(env, assignment, error, kind):
    artifact = env.publish()
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute('PRAGMA ignore_check_constraints=ON')
        connection.execute(f'UPDATE job_artifacts SET {assignment}')
        connection.commit()
    with pytest.raises(kind, match=f'^{error}$') as caught:
        env.service().open_reusable(artifact.artifact_id, env.request())
    assert 'canary' not in str(caught.value)


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
