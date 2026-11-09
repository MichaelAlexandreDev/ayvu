# ADR: Job checkpoints and derived artifacts

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-012` and `DEC-004`, scoped to job-owned artifact metadata, payload layout and publication

Related issue: [#137 — Store versioned job checkpoints and derived artifacts](https://github.com/MichaelAlexandreDev/ayvu/issues/137)

## Decision and scope

The project stores job-owned payloads as files and their metadata in the
[project database](local-project-storage.md), as schema version 3:

- `projects/<project UUID>/artifacts/staging/<artifact UUID>.part`: payload being written.
- `projects/<project UUID>/artifacts/ready/<artifact UUID>.bin`: published payload.
- Table `job_artifacts`: identity, ownership, lifecycle, size and digest.

A checkpoint is an artifact with its own `role`; there is no separate table. A final
output keeps this record and protocol but not this location:
[`TranslateProject`](translate-project.md#publishing-to-the-chosen-file) publishes it to
the file the user chose. The CLI does not use that yet, and pipeline stages do not change. Provenance, content
addressing, retention and cross-job reuse are not accepted here.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.artifact` | Domain | Identity, lifecycle, derived locations and the reuse predicate |
| `ayvu.artifact_service` | Application | Ports, publication order, reuse and reconciliation |
| `ayvu.artifact_store` | Infrastructure | SQLite repository, contained files and injectable file operations |
| `ayvu.legacy_resume` | Infrastructure | Read-only view of a legacy checkpoint |

## Lifecycle

```text
staging -> ready | abandoned
ready   -> abandoned   (explicit retirement after failed verification)
```

`abandoned` is final. An abandoned record frees its identity for a new attempt, which
gets a new artifact ID and location, so no attempt reuses another's path. A trigger
refuses any other change, including to identity columns and to size and digest once
recorded, as a second line of defense behind the domain rules.

Locations derive from the artifact UUID alone, so no title or file name reaches the
layout. A stored location is still compared with the derived one on every read, and
a difference raises `invalid_location` without touching that path.

## Publication protocol

SQLite and the filesystem cannot commit together, so this order leaves every
interruption in a state reconciliation can resolve:

1. Reserve a `staging` record. This requires the live lease of the owning job.
2. Write the temporary file while hashing, `fsync` it and its directory, and read it
   back to check size and digest. No transaction is open.
3. Record size and digest, still as `staging`.
4. Publish with a hard link, verify the final file, then remove the staging name.
5. Compare-and-swap the record to `ready`, which requires the lease again.

**Hard link instead of rename.** POSIX `rename` replaces an existing destination, and
publication must never overwrite anything. `os.link` fails if the destination exists,
and the migration runner already relies on it. This requires a local filesystem with
hard links.

A failure before step 4 abandons the record when the lease allows it. A failure from
step 4 on leaves it `staging`, because the file may already be visible and only
reconciliation can tell whether the publication completed. Cancellation is observed at
`artifact_before_write`, `artifact_between_chunks` and `artifact_before_publication`,
so it can never arrive after a file became visible. A `CancellationSource` passed to
`publish` must declare these names.

## Reconciliation

`reconcile()` is explicit and is never run by a read. It resolves `staging` records
whose job has no live lease, because a live lease may mean a publication in progress.
The lease is checked again inside the transaction that settles each record.

| Found | Result | Reason code |
| --- | --- | --- |
| Job has a live lease | Not touched; reported as `active` | |
| No recorded digest | `abandoned` | `interrupted_before_digest` |
| Digest recorded, no final file | `abandoned` | `interrupted_before_publication` |
| Final file matches, one name | `ready` | |
| Final file matches, staging name is the same inode | Staging name removed, then `ready` | |
| Final file differs, or has any other extra name | `abandoned` | `final_mismatch` |
| A symlink on the path | `abandoned` | `unsafe_location` |

The only payload name Ayvu ever removes is a staging name verified to be the same
inode as the published file. Every other file stays on disk for diagnosis, so
interrupted publications can consume disk space until a retention issue defines
cleanup. A publisher delayed past its lease can still link a file after its record was
abandoned. That file stays unreferenced and is never reported as ready, because the
final transition requires a live lease and a `staging` record.

## Reuse

`find_reusable` and `open_reusable` require a `ready` record with the same project,
job, role, media type, stage, stage version and input fingerprint, and then hash the
whole file. Existence never counts as validity. This costs a full read on each check,
which is significant on large files. `open_reusable` returns the handle it verified,
so the checked bytes are the bytes the caller reads. A hard-linked final file is
refused, since it could change through its other name.

Reads never change a record. A failed check reports its reason, and the record stays
`ready`, which also keeps blocking a new publication. `retire_if_corrupt` withdraws it
explicitly, after confirming the mismatch, so the work can be redone. The file stays.

## Path safety

Each operation checks every path component for symlinks before and after it acts,
opens with no-follow and checks the descriptor rather than a later lookup of the name.
Files use mode `0600` and directories `0700`. Newly created directories and the entries
inside them are synced. Non-regular files are refused, and opening a FIFO does not block.

These checks defend against mistakes and against untrusted names and stored values. As
for the rest of the workspace, they are not a sandbox against another process running
as the same user.

## Legacy resume files

`inspect_legacy_resume` reads a version 1 `*.ayvu-state.json` without writing, renaming
or deleting anything, and the legacy flow keeps working unchanged. It returns version,
status, languages and chapter counts. Paths, chapter names and the translator URL are
omitted, because they can identify a book or carry credentials. Errors are bounded
codes that quote nothing from the file.

Nothing is imported. [Job recovery](job-recovery.md) reads the same files through
`scan_legacy_states` to decide what to offer. A checkpoint is not a verified artifact, so
it is never reusable as one.

## Consequences and limits

- A payload is limited to 4 GiB and metadata to the database's 64 MiB limit.
- Reconciliation is called by whoever opens the project; nothing runs it automatically.
- Only the owning job reuses an artifact, so a continuation job redoes the work for now.
- `reconcile` fails closed if a stored record is invalid, rather than skipping it.
- Released migrations cannot be edited, so a schema correction has to be a new version.
- Native Windows behavior, network filesystems and filesystems without hard links have
  not been validated.
