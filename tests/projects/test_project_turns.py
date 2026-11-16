"""One run's project-database connections take turns in-process, so a reader never races a commit."""
from __future__ import annotations

import sqlite3
import time
from threading import Event, Thread

import pytest

from ayvu.kernel.sqlite import MigrationError
from ayvu.projects.schema import project_migration_runner
from ayvu.projects.turns import ProjectDatabaseTurns


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "project.sqlite"
    assert project_migration_runner().migrate(path, tmp_path / "backups").ok
    return path


def hold_exclusive(runner, database, held, seconds):
    connection = runner.connect(database, readonly=False)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        held.set()
        time.sleep(seconds)
        connection.execute("COMMIT")
    finally:
        connection.close()


def test_without_turns_a_reader_fails_while_another_connection_commits(database):
    held = Event()
    writer = Thread(target=hold_exclusive, args=(project_migration_runner(busy_timeout=0.05), database, held, 0.5))
    writer.start()
    assert held.wait(5)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            project_migration_runner(busy_timeout=0.05).connect(database, readonly=True).close()
    finally:
        writer.join()


def test_with_turns_a_reader_waits_for_the_commit_instead(database):
    turns, held = ProjectDatabaseTurns(), Event()
    runner = project_migration_runner(busy_timeout=0.05, turns=turns)
    writer = Thread(target=hold_exclusive, args=(runner, database, held, 0.5))
    writer.start()
    assert held.wait(5)
    started = time.monotonic()
    connection = runner.connect(database, readonly=True)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] > 0
    finally:
        connection.close()
    writer.join()
    assert time.monotonic() - started >= 0.3


def test_a_thread_holding_the_turn_may_open_another_connection(database):
    runner = project_migration_runner(turns=ProjectDatabaseTurns())
    outer = runner.connect(database, readonly=True)
    try:
        inner = runner.connect(database, readonly=True)
        inner.close()
    finally:
        outer.close()


def test_a_failed_connect_hands_the_turn_on(database, tmp_path):
    turns = ProjectDatabaseTurns()
    runner = project_migration_runner(turns=turns)
    with pytest.raises(MigrationError):
        runner.connect(tmp_path / "missing.sqlite", readonly=True)
    done = Event()

    def other_thread():
        runner.connect(database, readonly=True).close()
        done.set()
    Thread(target=other_thread, daemon=True).start()
    assert done.wait(5)


def test_separate_runs_do_not_wait_for_each_other(database):
    first = project_migration_runner(turns=ProjectDatabaseTurns()).connect(database, readonly=True)
    try:
        done = Event()

        def other_run():
            project_migration_runner(turns=ProjectDatabaseTurns()).connect(database, readonly=True).close()
            done.set()
        Thread(target=other_run, daemon=True).start()
        assert done.wait(5)
    finally:
        first.close()
