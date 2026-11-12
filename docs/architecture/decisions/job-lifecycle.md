# ADR: Durable translation job lifecycle

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Jobs (contract ownership)
Issue: [#136 — Persist the TranslationJob lifecycle](https://github.com/MichaelAlexandreDev/ayvu/issues/136)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-004`, scoped to the `TranslationJob` record, state machine and repository

## Decision and scope

Every long-running operation is modeled as a durable job, stored in the
[project database](local-project-storage.md) through the
[migration runner](../modules/projects-storage.md). A job records what was requested and
where it stands. It does not perform the work.

This acceptance is limited to job identity, lifecycle state, optimistic
concurrency, ownership lease, sanitized terminal summary and continuation link.
`DEC-004` also proposes persisted steps. `StepRun`, progress events, cancellation
contracts, checkpoints, artifacts, retry policy, scheduling and workers are not
accepted here. Progress, outcome and cancellation contracts were defined separately,
under `DEC-017`, in [job events and cancellation](../modules/jobs.md); they
persist nothing. Checkpoints and artifacts were accepted separately in
[job artifacts](job-artifacts.md). [`TranslateProject`](../translate-project.md) and
[job recovery](../modules/recovery.md) are what use jobs.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.jobs.model` | Domain | Immutable values, transition table, lease rules and typed errors |
| `ayvu.jobs.service` | Application | `JobRepository` port and `JobSnapshot` |
| `ayvu.jobs.store` | Infrastructure | SQLite adapter and the job schema SQL |
| `ayvu.projects.schema` | Infrastructure | Production migration registry and explicit repository bootstrap |

## Consequences and limits

Released migrations cannot be edited, because the ledger stores their checksum, so
any correction to the job schema has to be a new version. Until later issues adopt
this contract, it has no production caller. Native Windows behavior and network
filesystems have not been validated, as for the migration runner.

## Operational contract

The [module contract](../modules/jobs.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #136 scope; later integrations do not expand this acceptance.
