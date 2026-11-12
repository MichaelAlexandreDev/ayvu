import sqlite3
from contextlib import closing
import multiprocessing
import subprocess
import sys

import pytest
from ayvu.kernel import LanguagePair
from ayvu.kernel.sqlite import SqliteMigration, SqliteMigrationRunner

from tests.kernel.sqlite.sqlite_migrations_support import _crash_during_publication, seeded, state
from tests.support.paths import PROJECT_ROOT


def test_sqlite_vm_deadline_rolls_back_and_keeps_backup(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    original = path.read_bytes()
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only(value INTEGER)',
        'WITH RECURSIVE large(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM large WHERE x<100000000) SELECT sum(x) FROM large',
    )))
    result = SqliteMigrationRunner(registry, busy_timeout=0.01, budget_seconds=0.25).migrate(path, backups)
    assert not result.ok and result.error == 'deadline'
    assert path.read_bytes() == original


def test_real_project_boundary_and_cache_preservation(tmp_path):
    from ayvu.projects.store import ProjectJsonRepository, build_project_request
    from ayvu.v1.cache import TranslationCache, CacheKey
    source = tmp_path / 'original.epub'
    source.write_bytes(b'synthetic-original')
    repository = ProjectJsonRepository(tmp_path / 'processing')
    project = repository.create(build_project_request(source, source='en', target='pt'))
    paths = repository.resolve_database_paths(project.project_id)
    cache_path = tmp_path / 'legacy-cache.sqlite'
    with TranslationCache(cache_path) as cache:
        cache.set(CacheKey('synthetic-source', LanguagePair('en', 'pt')), 'synthetic-target')
    before = cache_path.read_bytes()
    result = SqliteMigrationRunner().migrate(paths.database, paths.backups)
    assert result.ok
    assert source.read_bytes() == b'synthetic-original'
    assert cache_path.read_bytes() == before
    declined = SqliteMigrationRunner().migrate(cache_path, tmp_path / 'cache-backups')
    assert not declined.ok and declined.error == 'unmanaged_database'
    assert cache_path.read_bytes() == before


def test_output_database_size_limit_is_enforced_before_commit(tmp_path, monkeypatch):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    before = path.read_bytes()
    monkeypatch.setattr('ayvu.kernel.sqlite.migrations.MAX_DATABASE_BYTES', 32768)
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only(value BLOB)',
        *('INSERT INTO test_only VALUES (zeroblob(4096))' for _ in range(16)),
    )))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert not result.ok and result.error == 'resource_limit'
    assert result.backup_verified
    assert path.read_bytes() == before


@pytest.mark.parametrize('upgrading', [False, True])
def test_interrupted_publication_preserves_complete_files_and_has_recovery_outcome(tmp_path, upgrading):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    if upgrading:
        seeded(path)
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=_crash_during_publication,
        args=(str(path), str(backups), ready, upgrading))
    try:
        process.start()
        assert ready.wait(timeout=10)
        process.kill()
        process.join(timeout=10)
        assert process.exitcode != 0
        registry = ((SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
                    if upgrading else None)
        result = SqliteMigrationRunner(registry).migrate(path, backups)
        assert not result.ok and result.error == 'recovery_required'
        directory = backups if upgrading else tmp_path
        pattern = '.backup-*.sqlite' if upgrading else '.migration-*.sqlite'
        stages = list(directory.glob(pattern))
        assert len(stages) == 1
        final = next(backups.glob('v*.sqlite')) if upgrading else path
        assert stages[0].samefile(final) and final.stat().st_nlink == 2
        assert state(final) == (1, [('ayvu_schema_migrations',)])
        # Explicit operator-equivalent action: drop only the duplicate staging link.
        stages[0].unlink()
        retry = SqliteMigrationRunner(registry).migrate(path, backups)
        assert retry.ok
        assert final.exists() and final.stat().st_nlink == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_multiple_steps_rollback_together_and_explicit_target_is_a_boundary(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    original = path.read_bytes()
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)),
                SqliteMigration(3, ('ALTER TABLE test_only ADD COLUMN another INTEGER', 'INVALID SQL')))
    runner = SqliteMigrationRunner(registry)
    failed = runner.migrate(path, backups)
    assert not failed.ok and failed.error == 'sql_failure'
    assert failed.from_version == failed.to_version == 1
    assert failed.applied == () and failed.backup_verified
    assert path.read_bytes() == original
    second = runner.migrate(path, backups, target_version=2)
    assert second.ok and second.applied == (2,)
    assert state(path) == (2, [('ayvu_schema_migrations',), ('test_only',)])
    current_bytes = path.read_bytes()
    third = runner.migrate(path, backups)
    assert not third.ok and third.from_version == third.to_version == 2
    assert path.read_bytes() == current_bytes
    assert third.backup_name == 'v00000002-to-00000003-000001.sqlite'


def test_fresh_publication_sync_error_is_explicit_and_retry_reconciles(tmp_path, monkeypatch):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    runner = SqliteMigrationRunner()
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError('synthetic-durability-failure')
        patch.setattr(runner, '_sync_directory', fail)
        result = runner.migrate(path, backups)
        assert not result.ok and result.error == 'publication_uncertain'
    assert state(path) == (1, [('ayvu_schema_migrations',)])
    assert runner.migrate(path, backups).status == 'unchanged'


def test_publication_never_overwrites_an_existing_destination(tmp_path, monkeypatch):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    runner = SqliteMigrationRunner()
    publish = runner._publish_unique
    def competing_file(staged, final):
        final.write_bytes(b'preserve-competing-file')
        publish(staged, final)
    monkeypatch.setattr(runner, '_publish_unique', competing_file)
    result = runner.migrate(path, backups)
    assert not result.ok
    assert path.read_bytes() == b'preserve-competing-file'
    assert not list(tmp_path.glob('.migration-*'))


def test_wal_database_is_rejected_without_backup_or_schema_mutation(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
    before = path.read_bytes()
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert not result.ok and result.error == 'unsupported_journal'
    assert path.read_bytes() == before
    assert state(path) == (1, [('ayvu_schema_migrations',)])
    assert not backups.exists()


@pytest.mark.parametrize('kind', ['valid', 'identity', 'checksum'])
def test_documented_snapshot_verifier_checks_registry_with_python_optimization(tmp_path, kind):
    path = tmp_path / 'snapshot.sqlite'
    seeded(path)
    with closing(sqlite3.connect(path)) as connection:
        if kind == 'identity':
            connection.execute('PRAGMA application_id=0')
        elif kind == 'checksum':
            connection.execute("UPDATE ayvu_schema_migrations SET checksum='invalid'")
        connection.commit()
    before = path.read_bytes()
    document = PROJECT_ROOT / 'docs' / 'architecture' / 'modules' / 'projects-storage.md'
    script = document.read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    result = subprocess.run([sys.executable, '-O', '-c', script, str(path)],
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is (kind == 'valid')
    if kind == 'valid':
        assert 'Snapshot integrity and ledger OK; version 1' in result.stdout
    else:
        assert 'Snapshot not verified; preserve all files' in result.stderr
        assert not result.stdout
    assert path.read_bytes() == before
