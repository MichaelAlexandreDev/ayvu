import sqlite3
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ayvu.sqlite_migrations import SqliteMigration, SqliteMigrationRunner


def state(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('PRAGMA user_version').fetchone()[0], connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()


def test_fresh_bootstrap_is_infrastructure_only_and_idempotent(tmp_path):
    path = tmp_path / 'project.sqlite'
    backups = tmp_path / 'backups' / 'schema'
    runner = SqliteMigrationRunner()
    first = runner.migrate(path, backups)
    assert first.ok and first.status == 'created'
    assert first.applied == (1,) and first.backup_name is None
    assert state(path) == (1, [('ayvu_schema_migrations',)])
    original = path.read_bytes()
    again = runner.migrate(path, backups)
    assert again.ok and again.status == 'unchanged'
    assert path.read_bytes() == original
    assert not backups.exists()


def test_existing_upgrade_has_verified_reopenable_pre_upgrade_backup(tmp_path):
    path = tmp_path / 'project.sqlite'
    backups = tmp_path / 'backups' / 'schema'
    assert SqliteMigrationRunner().migrate(path, backups).ok
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert result.ok and result.applied == (2,)
    assert result.backup_verified
    assert result.backup_name == 'v00000001-to-00000002-000001.sqlite'
    assert state(backups / result.backup_name) == (1, [('ayvu_schema_migrations',)])
    assert state(path)[0] == 2


def test_sql_failure_rolls_back_entire_upgrade_and_retains_backup(tmp_path):
    path = tmp_path / 'project.sqlite'
    backups = tmp_path / 'backups' / 'schema'
    assert SqliteMigrationRunner().migrate(path, backups).ok
    original = state(path)
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only(value INTEGER)', 'INVALID SQL private-canary',
    )))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert not result.ok and result.error == 'sql_failure'
    assert result.backup_verified
    assert state(path) == original
    assert state(backups / result.backup_name) == original
    assert 'private-canary' not in repr(result)


def seeded(path, version=1):
    fixture = Path(__file__).parent / 'fixtures' / 'migrations' / f'version-{version}.sql'
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(fixture.read_text())


@pytest.mark.parametrize('prior', [0, 1])
def test_each_actual_prior_fixture_upgrades_to_exact_target(tmp_path, prior):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path, prior)
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert result.ok and result.from_version == prior and result.to_version == 2
    assert result.backup_verified
    assert state(backups / result.backup_name)[0] == prior
    assert state(path) == (2, [('ayvu_schema_migrations',), ('test_only',)])
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)


@pytest.mark.parametrize('kind,error', [
    ('corrupt', 'integrity_failed'), ('newer', 'newer_schema'),
    ('gap', 'version_gap'), ('checksum', 'checksum_mismatch'), ('unmanaged', 'unmanaged_database'),
])
def test_rejection_precedes_backup_and_preserves_database(tmp_path, kind, error):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    if kind == 'corrupt':
        path.write_bytes(b'not a sqlite database: private-canary')
    else:
        seeded(path)
        with closing(sqlite3.connect(path)) as connection:
            if kind == 'newer':
                connection.execute('PRAGMA user_version=2')
            elif kind == 'gap':
                connection.execute('DELETE FROM ayvu_schema_migrations')
            elif kind == 'checksum':
                connection.execute("UPDATE ayvu_schema_migrations SET checksum='wrong'")
            else:
                connection.execute('PRAGMA application_id=0')
            connection.commit()
    before = path.read_bytes()
    result = SqliteMigrationRunner().migrate(path, backups)
    assert not result.ok and result.error == error
    assert path.read_bytes() == before
    assert not backups.exists()
    assert 'private-canary' not in repr(result)


@pytest.mark.parametrize('phase', ['create', 'write', 'publish', 'verify', 'verify_published'])
def test_backup_failure_prevents_schema_changes_and_keeps_previous_backups(tmp_path, monkeypatch, phase):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    backups.mkdir()
    prior_backup = backups / 'v00000000-to-00000001-000001.sqlite'
    seeded(prior_backup, 0)
    prior_bytes, original = prior_backup.read_bytes(), path.read_bytes()
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
    runner = SqliteMigrationRunner(registry)
    if phase == 'create':
        def fail(*args, **kwargs):
            raise OSError('private-canary')
        monkeypatch.setattr('ayvu.sqlite_migrations.tempfile.mkstemp', fail)
    elif phase == 'write':
        def partial(source, target, deadline):
            target.execute('CREATE TABLE incomplete(value INTEGER)')
            raise OSError('private-canary')
        monkeypatch.setattr(runner, '_copy_backup', partial)
    elif phase == 'publish':
        def fail(*args):
            raise OSError('private-canary')
        monkeypatch.setattr('ayvu.sqlite_migrations.os.link', fail)
    else:
        original_verify = runner._verify_backup
        def fail_verify(candidate, *args):
            if phase == 'verify' or not candidate.name.startswith('.backup-'):
                raise OSError('private-canary')
            original_verify(candidate, *args)
        monkeypatch.setattr(runner, '_verify_backup', fail_verify)
    result = runner.migrate(path, backups)
    assert not result.ok and result.error == 'backup_failed'
    assert path.read_bytes() == original
    assert prior_backup.read_bytes() == prior_bytes
    assert state(path) == (1, [('ayvu_schema_migrations',)])
    assert not result.backup_verified
    assert not list(backups.glob('.backup-*'))
    assert 'private-canary' not in repr(result)


def test_sqlite_writer_contention_returns_busy_without_migration(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute('BEGIN IMMEDIATE')
        registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
        result = SqliteMigrationRunner(registry, busy_timeout=0.01).migrate(path, backups)
        assert not result.ok and result.error == 'busy'
        holder.execute('ROLLBACK')
    assert state(path) == (1, [('ayvu_schema_migrations',)])
    assert not backups.exists()


@pytest.mark.parametrize('statement', ['COMMIT', 'VACUUM', 'PRAGMA user_version=9',
    'DROP TABLE ayvu_schema_migrations', "ATTACH DATABASE ':memory:' AS other"])
def test_migration_sql_cannot_escape_transaction_or_change_ledger(tmp_path, statement):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    original = path.read_bytes()
    registry = (SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)', statement)))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert not result.ok and result.error == 'sql_failure'
    assert path.read_bytes() == original
    assert result.backup_verified


def _concurrent_migration(path, backups, barrier, queue):
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only_counter(value INTEGER)', 'INSERT INTO test_only_counter VALUES (1)',
    )))
    barrier.wait(timeout=10)
    result = SqliteMigrationRunner(registry, busy_timeout=2).migrate(Path(path), Path(backups))
    queue.put((result.status, result.error, result.applied))


def test_two_processes_cannot_apply_one_migration_twice(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    context = multiprocessing.get_context('spawn')
    barrier, queue = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_concurrent_migration,
        args=(str(path), str(backups), barrier, queue)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(result[0] for result in results) == ['unchanged', 'upgraded']
        assert all(result[1] is None for result in results)
        assert sum(len(result[2]) for result in results) == 1
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute('SELECT count(*) FROM test_only_counter').fetchone() == (1,)
        assert len(list(backups.glob('*.sqlite'))) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


def _interrupted_upgrade(path, backups, ready):
    class PausedRunner(SqliteMigrationRunner):
        def _before_commit(self, connection):
            ready.set()
            # Parent kills the process after real SQL and before commit.
            import time
            time.sleep(20)
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only_blob(value BLOB)',
        'INSERT INTO test_only_blob VALUES (zeroblob(4194304))',
    )))
    PausedRunner(registry).migrate(Path(path), Path(backups))


def test_process_killed_before_commit_recovers_original_and_retains_backup(tmp_path):
    path, backups = tmp_path / 'project.sqlite', tmp_path / 'backups'
    seeded(path)
    original = path.read_bytes()
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=_interrupted_upgrade, args=(str(path), str(backups), ready))
    try:
        process.start()
        assert ready.wait(timeout=10)
        process.kill()
        process.join(timeout=10)
        assert process.exitcode != 0
        saved = list(backups.glob('*.sqlite'))
        assert len(saved) == 1
        assert state(saved[0]) == (1, [('ayvu_schema_migrations',)])
        registry = (SqliteMigration(1), SqliteMigration(2, (
            'CREATE TABLE test_only_blob(value BLOB)',
            'INSERT INTO test_only_blob VALUES (zeroblob(4194304))',
        )))
        # RO inspection must not silently perform a write-requiring recovery.
        stopped = SqliteMigrationRunner(registry).migrate(path, backups)
        assert not stopped.ok and stopped.error == 'recovery_required'
        # Explicit operator-equivalent RW reopen permits SQLite's native rollback.
        assert state(path) == (1, [('ayvu_schema_migrations',)])
        assert path.read_bytes() == original
        assert saved[0].exists()
        retry = SqliteMigrationRunner(registry).migrate(path, backups)
        assert retry.ok and state(path)[0] == 2
        assert len(list(backups.glob('*.sqlite'))) == 2
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


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
    from ayvu.project_store import ProjectJsonRepository, build_project_request
    from ayvu.cache import TranslationCache, CacheKey
    from ayvu.domain import LanguagePair
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
    monkeypatch.setattr('ayvu.sqlite_migrations.MAX_DATABASE_BYTES', 32768)
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only(value BLOB)',
        *('INSERT INTO test_only VALUES (zeroblob(4096))' for _ in range(16)),
    )))
    result = SqliteMigrationRunner(registry).migrate(path, backups)
    assert not result.ok and result.error == 'resource_limit'
    assert result.backup_verified
    assert path.read_bytes() == before


def _crash_during_publication(path, backups, ready, upgrading):
    class PublicationRunner(SqliteMigrationRunner):
        def _publish_unique(self, staged, final):
            os.link(staged, final)
            ready.set()
            import time
            time.sleep(20)
    registry = ((SqliteMigration(1), SqliteMigration(2, ('CREATE TABLE test_only(value INTEGER)',)))
                if upgrading else None)
    PublicationRunner(registry).migrate(Path(path), Path(backups))


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
    document = Path(__file__).parents[1] / 'docs' / 'architecture' / 'project-migrations.md'
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
