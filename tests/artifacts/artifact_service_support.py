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
from ayvu.artifacts.model import (Artifact, ArtifactCancelledError, ArtifactConflictError, ArtifactIntegrityError,
    ArtifactNotFoundError, ArtifactOwnershipError, ArtifactPathError, ArtifactRequest, ArtifactState,
    ArtifactStoreError, ArtifactValidationError)
from ayvu.artifacts.service import ARTIFACT_SAFE_POINTS, ArtifactService
from ayvu.artifacts.store import ArtifactFileOps, LocalArtifactPayloads, SqliteArtifactRepository
from ayvu.jobs.model import JobRequest, JobState
from ayvu.jobs.execution import CancellationSource
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_artifact_service, open_job_repository
from ayvu.kernel import JobCancellationError
from ayvu.kernel.sqlite import SqliteMigrationRunner

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


def _tamper(env, assignment):
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute(f'UPDATE job_artifacts SET {assignment}')
        connection.commit()
