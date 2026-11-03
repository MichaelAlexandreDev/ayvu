# ADR: Durable translation job lifecycle

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-004`, scoped to the `TranslationJob` record, state machine and repository

Related issue: [#136 — Persist the TranslationJob lifecycle](https://github.com/MichaelAlexandreDev/ayvu/issues/136)

## Decision and scope

Every long-running operation is modeled as a durable job, stored in the
[project database](local-project-storage.md) through the
[migration runner](project-migrations.md). A job records what was requested and
where it stands. It does not perform the work.

This acceptance is limited to job identity, lifecycle state, optimistic
concurrency, ownership lease, sanitized terminal summary and continuation link.
`DEC-004` also proposes persisted steps. `StepRun`, progress events, cancellation
contracts, checkpoints, artifacts, retry policy, scheduling and workers are not
accepted here. Progress, outcome and cancellation contracts were defined separately,
under `DEC-017`, in [job events and cancellation](job-events-cancellation.md); they
persist nothing. No interface uses jobs yet: translation, resume and the CLI keep
their current behavior, and translating a book still creates no project database.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.job` | Domain | Immutable values, transition table, lease rules and typed errors |
| `ayvu.job_service` | Application | `JobRepository` port and `JobSnapshot` |
| `ayvu.job_store` | Infrastructure | SQLite adapter and the job schema SQL |
| `ayvu.project_schema` | Infrastructure | Production migration registry and explicit repository bootstrap |

## Lifecycle

```text
queued     -> preflight | cancelled
preflight  -> running | failed
running    -> pausing | cancelling | partially_completed | failed | completed
pausing    -> paused
paused     -> queued
cancelling -> cancelled
completed  -> validating
validating -> validated | partially_completed
```

`validated`, `partially_completed`, `failed` and `cancelled` are immutable terminal
outcomes. `completed` is not terminal: it means processing ended, and validation
still has to run. A job in `preflight`, `pausing` or `paused` cannot move directly
to `cancelled`; a paused job returns to `queued` first.

Every nonterminal state has a path to a terminal outcome, so no job can get stuck.

## Compare-and-swap rules

A transition supplies the revision the caller last observed. The checks run in this
order, inside one short `BEGIN IMMEDIATE` transaction:

1. A different revision raises `JobRevisionConflictError`, whatever the target is.
2. The already-current state is a no-op that changes neither revision nor time.
   A supplied terminal summary is ignored in this case: the returned job holds the
   recorded one.
3. Any other edge outside the table raises `JobTransitionError`.
4. A permitted edge increments the revision and sets `updated_at`.

Rule 1 limits idempotency. A caller retrying after a lost reply still holds the old
revision, so it receives a conflict and must read the job again before deciding.

`updated_at` never moves backwards if the clock does.

## Ownership lease

An active owner holds an opaque generated token with an expiry of at most one hour.
Lease changes are not lifecycle history, so they change neither `revision` nor
`updated_at`; otherwise each renewal would make a concurrent pause or cancellation
request conflict. For the same reason, a transition checks only the revision and
does not require the owner token: cancellation comes from an interface, not from
the worker.

A lease is expired from the exact instant of `expires_at`. An expired lease on a
nonterminal job appears as `requires_reconciliation` in `JobSnapshot`. Reads use a
read-only connection, so loading never changes the job or claims an outcome. While
in this condition, acquiring, renewing and releasing all raise
`lease_requires_reconciliation`. Explicit transitions remain available, and entering
a terminal state clears the lease in the same transaction. Automatic reconciliation
belongs to [#146](https://github.com/MichaelAlexandreDev/ayvu/issues/146).

## Continuations

A retry requested after a terminal outcome is a new job. The link is stored on the
new job as `continuation_of`, which leaves the terminal record byte-for-byte
unchanged. A terminal job has at most one direct continuation, enforced by a unique
constraint, and it must belong to the same project. Its fingerprint may differ.

## Stored data and privacy

Schema version 2 adds table `translation_jobs`, one index and one trigger. The
trigger refuses any update to a terminal row and to immutable columns, as a second
line of defense behind the domain rules. Jobs stay in the project database, whose
limits and recovery procedures apply, including its 64 MiB limit.

Only UUIDs, slugs, counters, UTC timestamps and a `sha256:` input fingerprint are
accepted. The terminal summary is one reason code plus up to 16 named non-negative
integers, and the slug format excludes paths and free text. Credentials, prompts and
document text therefore have no field to enter. Errors are bounded codes, and default
`repr` output omits the owner token, the fingerprint and the counters. The fingerprint
and token remain readable by any process able to open the project database; this does
not defend against another process running as the same user.

Each operation opens its own connection, which repeats the application identity,
schema version and ledger checksum checks, but not the full integrity scan that
`migrate` performs. A row failing validation raises `invalid_job_record` rather than
being repaired. `busy` is a typed error, and retry policy belongs to the caller.

## Relationship to resume and control files

`*.ayvu-state.json` remains the only recovery mechanism in use, and `resume.py` keeps
its `running`/`completed` values. Job code does not import, read, rewrite or delete
those files, and a legacy checkpoint is not imported as a job. Moving recovery to
application use cases belongs to
[#146](https://github.com/MichaelAlexandreDev/ayvu/issues/146).

## Consequences and limits

Released migrations cannot be edited, because the ledger stores their checksum, so
any correction to the job schema has to be a new version. Until later issues adopt
this contract, it has no production caller. Native Windows behavior and network
filesystems have not been validated, as for the migration runner.
