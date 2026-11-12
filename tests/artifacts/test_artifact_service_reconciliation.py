import errno
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import sqlite3
from threading import Barrier, Event
from uuid import UUID

import pytest

from ayvu.artifacts.model import Artifact, ArtifactConflictError, ArtifactNotFoundError, ArtifactOwnershipError, ArtifactState, ArtifactStoreError
from ayvu.artifacts.service import ArtifactService
from ayvu.artifacts.store import LocalArtifactPayloads
from ayvu.jobs.model import JobState

from tests.artifacts.artifact_service_support import FaultOps, MINUTE
from tests.artifacts.artifact_service_support import OTHER_PROJECT, PAYLOAD
from tests.artifacts.artifact_service_support import SimulatedCrash, env, repository_of


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
