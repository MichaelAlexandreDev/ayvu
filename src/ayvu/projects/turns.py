"""Connections of one run to the project database take turns inside the process.

The project database uses SQLite's rollback journal. While one connection commits, every
other connection waits through its busy timeout, readers included. With several document
workers writing back to back, a reader can miss every gap between commits and fail with
``busy`` on a slow disk, however long the timeout. When every connection of the run holds
one shared turn until it closes, SQLite never sees two connections of this process at
once, and the busy timeout is left for other processes.
"""
from __future__ import annotations

from threading import RLock


class ProjectDatabaseTurns:
    """One per run; a thread that already holds the turn may open another connection."""

    def __init__(self) -> None:
        self._lock = RLock()

    def __repr__(self) -> str:
        return "ProjectDatabaseTurns()"

    def runner(self, runner) -> "TurnTakingRunner":
        return TurnTakingRunner(runner, self)


class TurnTakingRunner:
    """Delegates to the migration runner; each connection holds the turn until it closes.

    It keeps its ``ProjectDatabaseTurns`` alive, so a registry holding them weakly finds the
    same turns for as long as any store of the run exists.
    """

    def __init__(self, runner, turns: ProjectDatabaseTurns) -> None:
        self._runner, self._turns, self._lock = runner, turns, turns._lock

    def __getattr__(self, name):
        return getattr(self._runner, name)

    def connect(self, database, *, readonly: bool):
        self._lock.acquire()
        try:
            return _TurnConnection(self._runner.connect(database, readonly=readonly), self._lock)
        except BaseException:
            self._lock.release()
            raise


class _TurnConnection:
    """Delegates to the SQLite connection and hands the turn on when it closes."""

    def __init__(self, connection, lock) -> None:
        self._connection, self._lock = connection, lock

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            lock, self._lock = self._lock, None
            if lock is not None:
                lock.release()
