from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
import sqlite3
from threading import Barrier
from uuid import UUID
import pytest
from ayvu.jobs.model import (
    JobContinuationError, JobLeaseError, JobNotFoundError, JobRequest, JobRevisionConflictError, JobState, JobStoreError, JobTerminalSummary, JobTransitionError)
from ayvu.jobs.store import MAX_LIST_LIMIT, SqliteJobRepository
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_job_repository
from ayvu.kernel import JobValidationError
from ayvu.kernel.sqlite import SqliteMigrationRunner

PROJECT = str(UUID(int=1))


OTHER_PROJECT = str(UUID(int=99))


MISSING = str(UUID(int=98))


START = datetime(2026, 11, 3, 12, tzinfo=UTC)


MINUTE = timedelta(minutes=1)


# The shortest route to each state, so every serialized value can be persisted.
ROUTES = {
    JobState.QUEUED: (),
    JobState.PREFLIGHT: (JobState.PREFLIGHT,),
    JobState.RUNNING: (JobState.PREFLIGHT, JobState.RUNNING),
    JobState.PAUSING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PAUSING),
    JobState.PAUSED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PAUSING, JobState.PAUSED),
    JobState.CANCELLING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING),
    JobState.CANCELLED: (JobState.CANCELLED,),
    JobState.FAILED: (JobState.PREFLIGHT, JobState.FAILED),
    JobState.PARTIALLY_COMPLETED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.PARTIALLY_COMPLETED),
    JobState.COMPLETED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED),
    JobState.VALIDATING: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING),
    JobState.VALIDATED: (JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING,
                         JobState.VALIDATED),
}


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, delta=timedelta(seconds=1)):
        self.now += delta


class Sequence:
    def __init__(self, render):
        self.render = render
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.render(self.count)


class Store:
    def __init__(self, tmp_path):
        self.database = tmp_path / 'project.sqlite'
        self.backups = tmp_path / 'backups' / 'schema'
        self.clock = Clock()
        self.ids = Sequence(lambda n: str(UUID(int=1000 + n)))
        self.tokens = Sequence(lambda n: f'{n:064x}')
        self.repository = self.open()

    def open(self):
        return open_job_repository(self.database, self.backups, PROJECT, clock=self.clock,
                                   new_id=self.ids, new_token=self.tokens)

    def raw(self, sql, *parameters):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            rows = connection.execute(sql, parameters).fetchall()
            connection.commit()
            return rows

    def row(self, job_id):
        return self.raw('SELECT * FROM translation_jobs WHERE job_id = ?', job_id)

    def bytes(self):
        return self.database.read_bytes()

    def advance_to(self, job, state):
        for target in ROUTES[state]:
            self.clock.advance()
            job = self.repository.transition(job.job_id, target, expected_revision=job.revision)
        return job


def request(**changes):
    return JobRequest(**{'project_id': PROJECT, 'command_type': 'translate_epub', 'command_version': 1,
                         'input_fingerprint': 'sha256:' + 'a' * 64,
                         'output_roles': ('translated_epub', 'report'), **changes})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)
