"""The production schema history of the project database.

Each step's SQL stays with its owning module; this registry only orders them.
Released steps are immutable: a schema change is always a new version.
"""
from __future__ import annotations

from pathlib import Path

from .artifact import ArtifactStoreError
from .artifact_service import ArtifactPayloadStore, ArtifactService
from .artifact_store import ARTIFACT_SCHEMA_STATEMENTS, ArtifactFileOps, LocalArtifactPayloads, SqliteArtifactRepository
from .job import JobStoreError
from .job_store import JOB_SCHEMA_STATEMENTS, SqliteJobRepository
from .provenance import ProvenanceStoreError
from .provenance_store import PROVENANCE_SCHEMA_STATEMENTS, SqliteProvenanceRepository
from .sqlite_migrations import SqliteMigration, SqliteMigrationRunner

PROJECT_MIGRATIONS = (
    SqliteMigration(1),
    SqliteMigration(2, JOB_SCHEMA_STATEMENTS),
    SqliteMigration(3, ARTIFACT_SCHEMA_STATEMENTS),
    SqliteMigration(4, PROVENANCE_SCHEMA_STATEMENTS),
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


def open_artifact_service(database: Path, backups: Path, artifact_root: Path, project_id: str, *,
                          ops: ArtifactFileOps | None = None, payloads: ArtifactPayloadStore | None = None,
                          **factories) -> ArtifactService:
    """Bootstrap or upgrade the database explicitly, then expose the project's artifacts.

    ``factories`` are the optional clock/new_id of the repository. ``payloads`` replaces the project's
    own files, for an output the user chose. ``ops`` then belongs to that store, not to this call.
    """
    runner = project_migration_runner()
    result = runner.migrate(database, backups)
    if not result.ok:
        raise ArtifactStoreError(result.error or "io")
    repository = SqliteArtifactRepository(runner, database, project_id, **factories)
    return ArtifactService(repository, payloads or LocalArtifactPayloads(artifact_root, ops=ops))


def open_provenance_repository(database: Path, backups: Path, project_id: str, **factories) -> SqliteProvenanceRepository:
    """Bootstrap or upgrade the database explicitly, then expose the project's lineage.

    ``factories`` are the optional clock/new_id of the repository.
    """
    runner = project_migration_runner()
    result = runner.migrate(database, backups)
    if not result.ok:
        raise ProvenanceStoreError(result.error or "io")
    return SqliteProvenanceRepository(runner, database, project_id, **factories)
