from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import UUID

import pytest

from ayvu.job import JobRequest, JobState, JobStoreError
from ayvu.job_store import JOB_SCHEMA_STATEMENTS
from ayvu.project_schema import PROJECT_MIGRATIONS, open_job_repository, project_migration_runner
from ayvu.project_store import ProjectJsonRepository, build_project_request
from ayvu.sqlite_migrations import APPLICATION_ID, SqliteMigrationRunner

FIXTURES = Path(__file__).parent / 'fixtures' / 'migrations'
PROJECT = str(UUID(int=1))
V2_CHECKSUM = 'bb8de968c66ff715f48f6fd763be29a553fd4c6a49b14c702dc07b1dabe5e5d9'


def seeded(path, version):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((FIXTURES / f'version-{version}.sql').read_text())


def schema(path):
    with closing(sqlite3.connect(path)) as connection:
        return (connection.execute('PRAGMA user_version').fetchone()[0],
                connection.execute('PRAGMA application_id').fetchone()[0],
                connection.execute('SELECT type, name, sql FROM sqlite_schema ORDER BY type, name').fetchall(),
                connection.execute('SELECT version, checksum FROM ayvu_schema_migrations ORDER BY version').fetchall())


def request():
    return JobRequest(PROJECT, 'translate_epub', 1, 'sha256:' + 'a' * 64)


def test_registry_history_is_ordered_and_released_steps_are_frozen():
    assert [step.version for step in PROJECT_MIGRATIONS] == [1, 2]
    assert PROJECT_MIGRATIONS[0].statements == ()
    assert PROJECT_MIGRATIONS[1].statements == JOB_SCHEMA_STATEMENTS
    # A failure here means a released migration was edited. Add a new version instead.
    assert PROJECT_MIGRATIONS[1].checksum == V2_CHECKSUM


def test_default_runner_keeps_its_infrastructure_only_registry():
    assert [step.version for step in SqliteMigrationRunner().registry] == [1]


def test_fresh_bootstrap_reaches_the_released_schema_and_is_idempotent(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    runner = project_migration_runner()
    first = runner.migrate(path, backups)
    assert first.ok and first.status == 'created' and first.applied == (1, 2)
    original = path.read_bytes()
    assert runner.migrate(path, backups).status == 'unchanged'
    assert path.read_bytes() == original and not backups.exists()
    version, application, objects, ledger = schema(path)
    assert (version, application) == (2, APPLICATION_ID)
    assert {name for _, name, _ in objects} >= {
        'ayvu_schema_migrations', 'translation_jobs', 'translation_jobs_by_project_state', 'translation_jobs_immutable',
    }
    assert ledger[1] == (2, V2_CHECKSUM)


def test_released_fixture_matches_a_fresh_bootstrap_and_needs_no_migration(tmp_path):
    fresh, released = tmp_path / 'fresh.sqlite', tmp_path / 'released.sqlite'
    assert project_migration_runner().migrate(fresh, tmp_path / 'fresh-backups').ok
    seeded(released, 2)
    assert schema(released) == schema(fresh)
    result = project_migration_runner().migrate(released, tmp_path / 'backups')
    assert result.ok and result.status == 'unchanged'


@pytest.mark.parametrize('prior', [0, 1])
def test_each_prior_version_upgrades_with_a_verified_backup(tmp_path, prior):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path, prior)
    before = schema(path) if prior else None
    result = project_migration_runner().migrate(path, backups)
    assert result.ok and result.status == 'upgraded' and result.to_version == 2 and result.backup_verified
    with closing(sqlite3.connect(backups / result.backup_name)) as snapshot:
        assert snapshot.execute('PRAGMA user_version').fetchone()[0] == prior
    if prior:
        assert schema(backups / result.backup_name) == before
    fresh = tmp_path / 'fresh.sqlite'
    assert project_migration_runner().migrate(fresh, tmp_path / 'fresh-backups').ok
    assert schema(path) == schema(fresh)


def test_upgrade_from_v1_then_jobs_work_end_to_end(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path, 1)
    repository = open_job_repository(path, backups, PROJECT)
    job = repository.create(request())
    assert repository.get(job.job_id).job.state is JobState.QUEUED
    assert len(list(backups.iterdir())) == 1


def test_infrastructure_only_runner_refuses_a_job_schema_database_unchanged(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    assert project_migration_runner().migrate(path, backups).ok
    before = path.read_bytes()
    result = SqliteMigrationRunner().migrate(path, backups)
    assert not result.ok and result.error == 'newer_schema'
    assert path.read_bytes() == before


def test_open_job_repository_reports_a_failed_migration_as_a_typed_error(tmp_path):
    path = tmp_path / 'project.sqlite'
    path.write_bytes(b'not a sqlite database: private-canary')
    with pytest.raises(JobStoreError) as caught:
        open_job_repository(path, tmp_path / 'backups', PROJECT)
    assert str(caught.value) == 'integrity_failed'
    assert path.read_bytes() == b'not a sqlite database: private-canary'


def test_real_project_boundary_preserves_manifest_source_and_cache(tmp_path):
    source = tmp_path / 'source.epub'
    source.write_bytes(b'synthetic-original')
    projects = ProjectJsonRepository(tmp_path / 'processing')
    project = projects.create(build_project_request(source, source='en', target='pt'))
    paths = projects.resolve_database_paths(project.project_id)
    manifest = paths.database.with_name('project.json')
    before = manifest.read_bytes()

    jobs = open_job_repository(paths.database, paths.backups, project.project_id)
    job = jobs.create(JobRequest(project.project_id, 'translate_epub', 1, 'sha256:' + 'a' * 64))

    assert jobs.get(job.job_id).job == job
    assert manifest.read_bytes() == before
    assert source.read_bytes() == b'synthetic-original'
    assert projects.load(project.project_id) == project


def test_documented_snapshot_verifier_accepts_the_current_schema(tmp_path):
    path = tmp_path / 'snapshot.sqlite'
    seeded(path, 2)
    document = Path(__file__).parents[1] / 'docs' / 'architecture' / 'project-migrations.md'
    script = document.read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    result = subprocess.run([sys.executable, '-O', '-c', script, str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'Snapshot integrity and ledger OK; version 2' in result.stdout
