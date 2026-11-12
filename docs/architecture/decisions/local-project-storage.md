# ADR: Local project SQLite storage and migration locations

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Projects (contract ownership); Kernel SQLite migration infrastructure
Issue: [#135 — Safe local migrations, backup and recovery](https://github.com/MichaelAlexandreDev/ayvu/issues/135)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-012`, scoped to project-database bootstrap and migrations

## Decision and scope

Use Python's standard-library SQLite adapter for the initial relational project
store, alongside the existing filesystem and version-1 JSON manifest. No server
or new Python dependency is required.

The managed project repository owns these locations under its configured
processing root:

- `projects/<project UUID>/project.json`: existing canonical manifest, unchanged.
- `projects/<project UUID>/project.sqlite`: relational database.
- `projects/<project UUID>/backups/schema/`: retained schema-upgrade snapshots.

The Projects adapter resolves and validates database/backup locations from the
canonical project UUID and trusted workspace. This is an explicit additive
extension to the repository delivered by #134, not an assertion that its original
port already exposed a database path. The migration runner receives resolved
paths; it does not select a project layout or reinterpret document content.

The first production database contains only migration-owned infrastructure and
its schema-version ledger. Future domain tables retain their owning modules.
The JSON manifest schema does not change and does not become a database locator.

## Consequences and limits

Projects gain local database and retained-backup disk usage. Directory ownership,
alias prevention, concurrent writers, transaction boundaries and failed publication
are part of the storage contract. A verified backup is recovery evidence, not
permission to overwrite another artifact.

This acceptance is limited to SQLite/filesystem persistence for #135's bootstrap
and migrations. It does not accept jobs (`DEC-004`), cache/TM/corpus design
(`DEC-013`), provenance or any other proposed decision. It does not migrate the
standalone translation cache, create domain tables, replace the manifest, or
change the existing translation CLI. Backup retention is manual until separately
reviewed lifecycle work exists. Jobs were later accepted separately, in the scoped
[job lifecycle ADR](job-lifecycle.md), and are schema version 2. Artifact metadata and
the `artifacts/` directories were accepted in the scoped
[job artifacts ADR](job-artifacts.md), and are schema version 3. Provenance was accepted
in the scoped [provenance ADR](provenance.md), and is schema version 4.

The [governed translation memory](../modules/knowledge-memory.md) introduced by
#161 uses a dedicated `approved-memory.sqlite` alongside `project.sqlite`, with
its own migration ledger and `backups/approved-memory-schema/`. It does not change
the project schema or migrate the operational cache.

This ADR records the accepted direction. Concrete migration behavior, validation
evidence and platform limitations belong to the implementation contract.

## Operational contract

The [module contract](../modules/projects-storage.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #135 scope; later integrations do not expand this acceptance.
