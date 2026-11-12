# ADR: Job checkpoints and derived artifacts

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Artifacts (job-owned artifact contract)
Issue: [#137 — Store versioned job checkpoints and derived artifacts](https://github.com/MichaelAlexandreDev/ayvu/issues/137)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-012` and `DEC-004`, scoped to job-owned artifact metadata, payload layout and publication

## Decision and scope

The project stores job-owned payloads as files and their metadata in the
[project database](local-project-storage.md), as schema version 3:

- `projects/<project UUID>/artifacts/staging/<artifact UUID>.part`: payload being written.
- `projects/<project UUID>/artifacts/ready/<artifact UUID>.bin`: published payload.
- Table `job_artifacts`: identity, ownership, lifecycle, size and digest.

A checkpoint is an artifact with its own `role`; there is no separate table. A final
output keeps this record and protocol but not this location:
[`TranslateProject`](../modules/translation-project.md#publishing-to-the-chosen-file) publishes it to
the file the user chose. The CLI does not use that yet, and pipeline stages do not change. Provenance, content
addressing, retention and cross-job reuse are not accepted here.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.artifacts.model` | Domain | Identity, lifecycle, derived locations and the reuse predicate |
| `ayvu.artifacts.service` | Application | Ports, publication order, reuse and reconciliation |
| `ayvu.artifacts.store` | Infrastructure | SQLite repository, contained files and injectable file operations |
| `ayvu.recovery.checkpoint_scan` | Infrastructure | Read-only view of a legacy checkpoint |

## Consequences and limits

- A payload is limited to 4 GiB and metadata to the database's 64 MiB limit.
- Reconciliation is called by whoever opens the project; nothing runs it automatically.
- Only the owning job reuses an artifact, so a continuation job redoes the work for now.
- `reconcile` fails closed if a stored record is invalid, rather than skipping it.
- Released migrations cannot be edited, so a schema correction has to be a new version.
- Native Windows behavior, network filesystems and filesystems without hard links have
  not been validated.

## Operational contract

The [module contract](../modules/artifacts.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #137 scope; later integrations do not expand this acceptance.
