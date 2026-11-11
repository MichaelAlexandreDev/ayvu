import sqlite3
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import pytest
from ayvu.kernel import LanguagePair
from ayvu.kernel.sqlite import SqliteMigration, SqliteMigrationRunner
from tests.support.paths import TESTS_ROOT

def state(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('PRAGMA user_version').fetchone()[0], connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()


def seeded(path, version=1):
    fixture = TESTS_ROOT / 'fixtures' / 'migrations' / f'version-{version}.sql'
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(fixture.read_text())


def _concurrent_migration(path, backups, barrier, queue):
    registry = (SqliteMigration(1), SqliteMigration(2, (
        'CREATE TABLE test_only_counter(value INTEGER)', 'INSERT INTO test_only_counter VALUES (1)',
    )))
    barrier.wait(timeout=10)
    result = SqliteMigrationRunner(registry, busy_timeout=2).migrate(Path(path), Path(backups))
    queue.put((result.status, result.error, result.applied))


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
