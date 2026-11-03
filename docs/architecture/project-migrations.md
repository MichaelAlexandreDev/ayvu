# Project SQLite bootstrap, upgrades and recovery

This contract implements [#135](https://github.com/MichaelAlexandreDev/ayvu/issues/135)
under the [accepted, scoped DEC-012](local-project-storage.md). The runner itself
owns no domain tables. Schema version 2 adds the job tables of the
[job lifecycle](job-lifecycle.md) and version 3 the [job artifact](job-artifacts.md)
metadata, and version 4 the [provenance](provenance.md) tables. No cache or review
tables exist.

## Composition boundary and current schema

The concrete Projects adapter, `ProjectJsonRepository.resolve_database_paths(id)`,
loads the real manifest, validates the UUID and paths, and returns a private
`ProjectDatabasePaths` object. It creates neither a database nor backup directory.
The framework-neutral `ProjectRepository` port retains resolve/create/load/save;
filesystem paths are deliberately a privileged composition concern, not an
application DTO. The manifest JSON v1 remains byte-for-byte unchanged by resolution.

```python
from ayvu.project_schema import project_migration_runner
from ayvu.project_store import ProjectJsonRepository

repository = ProjectJsonRepository(configured_processing_root)
paths = repository.resolve_database_paths(project_id)
result = project_migration_runner().migrate(paths.database, paths.backups)
```

These calls belong at an infrastructure/composition boundary. Ordinary translation
still creates/reuses its existing JSON project; it does not automatically create
the SQLite database. Future job services can explicitly request bootstrap through
their own reviewed composition. The runner never chooses layout or reads an EPUB.

The production history is `PROJECT_MIGRATIONS` in `ayvu.project_schema`, whose
current schema is version 4. A bare `SqliteMigrationRunner()` only knows version 1
and refuses a newer database with `newer_schema`, so production code must use
`project_migration_runner()`. Application ID is `0x41595655`, and
`PRAGMA user_version` holds the schema version. Table
`ayvu_schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)` is the
ledger. Ledger rows are contiguous from 1 and contain SHA-256 of each allowlisted
step's ordered SQL text. Version 1 has no domain SQL. Version 2 adds
`translation_jobs`, its index and its immutability trigger. Version 3 adds
`job_artifacts`, its indexes and its immutability trigger. Version 4 adds the
`provenance_records`, `provenance_parents` and `provenance_artifacts` tables, their
indexes and their append-only triggers. A released step is never edited; a schema change is always a new version.
Changed historical checksums, gaps and newer versions are rejected. No downgrade
or implicit adoption of another application's database is allowed.

The committed fixtures are an empty version-0 SQLite database, the version-1 ledger
and the released version-2, version-3 and version-4 schemas, represented by SQL under
`tests/fixtures/migrations/`. Tests of the runner itself use explicitly synthetic
harness migrations, not product history. Registries are ordered in-tree `SqliteMigration`
values, not filesystem/plugin discovery or document-supplied SQL.

## Transaction and verified-backup protocol

The runner serializes cooperating migrators with a per-database advisory file
lock. It first inspects an existing database in read-only mode: integrity,
version, application identity, ledger and checksums must pass before opening a
writer or creating a backup. Rejected files remain unchanged.

For an upgrade, a writer obtains `BEGIN IMMEDIATE` with a short busy timeout and
repeats validation. This excludes other SQLite writers while allowing the
separate read connection used for backup. The snapshot is never made through the
connection holding the write transaction. This follows the
[SQLite transaction rules](https://sqlite.org/lang_transaction.html) and uses the
[SQLite Online Backup API](https://sqlite.org/backup.html).

Before any schema SQL, the backup API writes a private temporary database inside
the supplied backup directory. The runner closes, checks integrity and version,
fsyncs, publishes without overwriting an existing name, then reopens and verifies
the published snapshot. Any failed stage prevents migration. Verified snapshots
are retained on subsequent SQL, timeout, commit or process failures.

All steps pending up to the selected target execute in **one transaction**.
Schema changes, ledger inserts and the version marker commit together. Failure
in a later step rolls the entire invocation back to its initial version, even if
an earlier pending step succeeded. `target_version` can select an earlier known
boundary explicitly; a subsequent invocation starts from that committed version.
SQL executes statement-by-statement, never through `executescript`. An authorizer
denies transaction control, PRAGMA, attached databases, virtual tables, extension
loading and step-owned changes to the reserved ledger. The runner alone updates
the ledger and version. The explicit transaction behavior is supported by the
[Python sqlite3 API](https://docs.python.org/3.11/library/sqlite3.html).

Fresh bootstrap builds and verifies a private staging database, then atomically
installs the complete file without replacing any existing destination. Publication
uses a same-filesystem hard link followed by removal of the staging name. A crash
between these operations leaves a recoverable pair, described below, rather than
an incomplete database. This requires a local filesystem supporting hard links,
SQLite locking and atomic directory-entry operations.

## Results, retention and limits

`MigrationResult` contains status (`created`, `upgraded`, `unchanged` or `failed`),
known versions, committed steps, an allowlisted error code, and a sanitized backup
name/verification flag. Absolute paths, SQL, source text and SQLite exceptions are
absent. The backup name is excluded from default repr. Failed results never imply
that a migration target was reached; reload after uncertain publication.

Backups are relative to the supplied project backup directory and named
`v<from:08d>-to-<target:08d>-<sequence:06d>.sqlite`. The next unused sequence is
chosen under the migration lock. No snapshot is overwritten or automatically
pruned. At 1024 backup entries the runner stops; retention changes require an
explicit preservation/deletion decision outside this issue.

The initial limits are 64 schema versions, 128 statements per version, 64 KiB
characters per statement and a 64 MiB database/snapshot. SQLite value size and the
logical post-migration page count are checked before commit so an upgrade cannot
publish a database the next invocation would reject. Resource failures roll back.

Job operations reuse these checks through `SqliteMigrationRunner.connect`, which
repeats identity, version and ledger checks but not the integrity scan.

Default contention bound is 0.5 seconds (configurable up to 2 seconds); default
operation budget is 5 seconds (up to 30). Progress handlers bound SQLite VM work
and backup iterations. These are cooperative limits, not guarantees against a
kernel/filesystem call that stalls. This first protocol accepts rollback journal
mode `delete`; WAL-mode databases return `unsupported_journal` without migration.
Native WAL migrations require separately tested support.

Project paths and sidecars reject symbolic links, unexpected hardlinks and
nonregular files. POSIX ancestors must be owned by the current user/root and
cannot be writable by other users without sticky-directory protection. Private
temporary files use mode 0600; project directories already use 0700. Windows ACLs,
native byte locks and directory durability have not been validated by Linux CI.
Network filesystems and hostile same-user processes are not sandboxed or claimed
as supported migration environments.

## Operator recovery: preserve before changing anything

Recovery is manual. Stop all Ayvu/SQLite processes that access the project. Keep a
private copy of the complete project directory, including the manifest, database,
backups and any `project.sqlite-journal`, `-wal` or `-shm` sidecars. Do not delete
sidecars or copy a live database file by itself. Never replace an original EPUB,
translation, standalone cache or another project's database during recovery.

### Inspect a snapshot without modifying it

Select the exact retained filename from the result and the approved project
layout. This local operator example prints only validation/version information;
use the Python environment with the compatible Ayvu registry installed and
replace the placeholder with that snapshot's path:

```bash
python3 - /path/to/projects/UUID/backups/schema/SNAPSHOT.sqlite <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from ayvu.project_schema import PROJECT_MIGRATIONS
from ayvu.sqlite_migrations import APPLICATION_ID

try:
    path = Path(sys.argv[1]).resolve(strict=True)
    registry = PROJECT_MIGRATIONS
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check(1)').fetchall() != [('ok',)]:
            raise ValueError('integrity_failed')
        if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise ValueError('unmanaged_database')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if not 1 <= version <= len(registry):
            raise ValueError('unsupported_version')
        rows = db.execute('SELECT version, checksum FROM ayvu_schema_migrations ORDER BY version').fetchmany(65)
        if rows != [(step.version, step.checksum) for step in registry[:version]]:
            raise ValueError('invalid_ledger')
    print('Snapshot integrity and ledger OK; version', version)
except (sqlite3.Error, ValueError, OSError):
    raise SystemExit('Snapshot not verified; preserve all files') from None
PY
```

A newer snapshot still requires a compatible application/registry; integrity
alone does not authorize a downgrade or prove that unknown SQL is trustworthy.

### `recovery_required`: SQLite hot journal

Read-only inspection may require native SQLite journal rollback, which needs
write access. This is a recoverable state, not automatically corruption. After
preservation and with all writers stopped, deliberately reopen the **existing**
`project.sqlite` with `mode=rw` (never the create-if-missing mode), disable trusted
schema, run `PRAGMA integrity_check(1)` and close. Opening/reading this way permits
SQLite's native rollback. The operation is distinct from restoring a backup.
Verify the restored version and ledger read-only before retrying the runner.
If native recovery or integrity fails, preserve the files and stop; do not remove
the journal or repeatedly attempt migrations on an unverified database.

### `recovery_required`: interrupted hard-link publication

An interruption can leave a complete final file and its `.migration-*.sqlite`
or `.backup-*.sqlite` staging name referring to the same inode. The runner stops
without deleting either. The Projects resolver also refuses a hardlinked database
with `project_database_recovery_required` until the alias is understood.

With writers stopped and files preserved, check that the two exact names are
regular, nonsymlink files inside the expected project/staging directory, have the
same device/inode, and have link count exactly two. One must be the recognized
staging name and the other the expected final database or versioned backup name.
Verify the final file read-only as above. Only then remove the **staging directory
entry**, retaining the final name and its bytes. Confirm link count one and retry.
For any unmatched alias, unknown name or failed verification, stop instead of
unlinking or promoting anything. An unpaired staging file is never authoritative.

### SQL, backup or uncertain-publication failures

`sql_failure`, `deadline`, `resource_limit` and failed backup operations do not
approve a target version. Preserve the pre-upgrade snapshot and verify the source
before retrying with a corrected registry/budget. `busy` requires releasing the
other writer, not deleting lock files. `publication_uncertain` means complete new
bytes may already be visible; inspect/reload rather than overwriting them.

If manual snapshot restoration is necessary, first verify the snapshot, then copy
it to a **new, nonexisting recovery-candidate filename**, using a no-clobber copy
operation. Reopen that candidate read-only and verify integrity/version/ledger.
Keep the failed original and its sidecars together at a separate preserved
location before deliberately promoting the verified candidate to `project.sqlite`.
No runner API performs this replacement or deletes the preserved files. Keep all
verified backups until a separate retention decision is reviewed.
