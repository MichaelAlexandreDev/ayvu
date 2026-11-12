import errno
import os
from contextlib import closing
import sqlite3
import stat

import pytest

from ayvu.artifacts.model import ArtifactIntegrityError, ArtifactState, ArtifactStoreError, ArtifactValidationError
from ayvu.artifacts.service import ArtifactService
from ayvu.artifacts.store import LocalArtifactPayloads

from tests.artifacts.artifact_service_support import CRASH_POINTS, CrashingRepository, DIGEST, FaultOps
from tests.artifacts.artifact_service_support import PAYLOAD, SIZE
from tests.artifacts.artifact_service_support import SimulatedCrash, env, repository_of


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
    monkeypatch.setattr('ayvu.artifacts.store.MAX_ARTIFACT_BYTES', 4)
    consumed = []

    def chunks():
        for chunk in (b'123', b'456', b'789'):
            consumed.append(chunk)
            yield chunk

    with pytest.raises(ArtifactValidationError, match='^artifact_too_large$'):
        env.publish(chunks=chunks())
    assert len(consumed) == 2 and env.rows()[0][1:3] == ('abandoned', 'artifact_too_large')
