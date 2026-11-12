import sqlite3
from contextlib import closing
import multiprocessing

import pytest
from ayvu.kernel.sqlite import SqliteMigration, SqliteMigrationRunner

from tests.kernel.sqlite.sqlite_migrations_support import _concurrent_migration, _interrupted_upgrade, seeded, state


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
        monkeypatch.setattr('ayvu.kernel.sqlite.migrations.tempfile.mkstemp', fail)
    elif phase == 'write':
        def partial(source, target, deadline):
            target.execute('CREATE TABLE incomplete(value INTEGER)')
            raise OSError('private-canary')
        monkeypatch.setattr(runner, '_copy_backup', partial)
    elif phase == 'publish':
        def fail(*args):
            raise OSError('private-canary')
        monkeypatch.setattr('ayvu.kernel.sqlite.migrations.os.link', fail)
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
