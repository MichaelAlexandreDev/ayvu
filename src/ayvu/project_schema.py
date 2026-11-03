"""The production schema history of the project database.

Each step's SQL stays with its owning module; this registry only orders them.
Released steps are immutable: a schema change is always a new version.
"""
from __future__ import annotations

from pathlib import Path

from .job import JobStoreError
from .job_store import JOB_SCHEMA_STATEMENTS, SqliteJobRepository
from .sqlite_migrations import SqliteMigration, SqliteMigrationRunner

PROJECT_MIGRATIONS = (
    SqliteMigration(1),
    SqliteMigration(2, JOB_SCHEMA_STATEMENTS),
)


def project_migration_runner() -> SqliteMigrationRunner:
    return SqliteMigrationRunner(PROJECT_MIGRATIONS)


def open_job_repository(database: Path, backups: Path, project_id: str, **factories) -> SqliteJobRepository:
    """Bootstrap or upgrade the database explicitly, then expose its jobs.

    ``factories`` are the optional clock/new_id/new_token of the repository.
    """
    runner = project_migration_runner()
    result = runner.migrate(database, backups)
    if not result.ok:
        raise JobStoreError(result.error or "io")
    return SqliteJobRepository(runner, database, project_id, **factories)
