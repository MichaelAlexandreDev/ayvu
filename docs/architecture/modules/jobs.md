# Jobs

The accepted scope is recorded in the [decision](../decisions/job-lifecycle.md).

The scope and consequences below preserve the implementation stage recorded in #136;
statements about later integrations describe that stage, not their current delivery status.

## Decision and scope

Every long-running operation is modeled as a durable job, stored in the
[project database](../decisions/local-project-storage.md) through the
[migration runner](projects-storage.md). A job records what was requested and
where it stands. It does not perform the work.

This acceptance is limited to job identity, lifecycle state, optimistic
concurrency, ownership lease, sanitized terminal summary and continuation link.
`DEC-004` also proposes persisted steps. `StepRun`, progress events, cancellation
contracts, checkpoints, artifacts, retry policy, scheduling and workers are not
accepted here. Progress, outcome and cancellation contracts were defined separately,
under `DEC-017`, in [job events and cancellation](#events-outcomes-and-cooperative-cancellation); they
persist nothing. Checkpoints and artifacts were accepted separately in
[job artifacts](../decisions/job-artifacts.md). [`TranslateProject`](translation-project.md) and
[job recovery](recovery.md) are what use jobs.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.jobs.model` | Domain | Immutable values, transition table, lease rules and typed errors |
| `ayvu.jobs.service` | Application | `JobRepository` port and `JobSnapshot` |
| `ayvu.jobs.store` | Infrastructure | SQLite adapter and the job schema SQL |
| `ayvu.projects.schema` | Infrastructure | Production migration registry and explicit repository bootstrap |

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
a terminal state clears the lease in the same transaction.
[Recovery](recovery.md) reads this condition and leaves acting on it to a run.

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

`recovery/checkpoint.py` keeps its `running`/`completed` values. The job modules do not import, read,
rewrite or delete `*.ayvu-state.json`, and a legacy checkpoint is not imported as a job.
[Job recovery](recovery.md) reads them, as a record of how a run was invoked, and
checks them against a job's fingerprint before believing them. A file that recomputes to
no job still runs on the current flow, with no job.

## Consequences and limits

Released migrations cannot be edited, because the ledger stores their checksum, so
any correction to the job schema has to be a new version. Until later issues adopt
this contract, it has no production caller. Native Windows behavior and network
filesystems have not been validated, as for the migration runner.

## Events, outcomes and cooperative cancellation

This contract implements [#126](https://github.com/MichaelAlexandreDev/ayvu/issues/126)
under the shared application contracts of `DEC-017`, recorded in the
[modular-monolith ADR](../decisions/modular-monolith-boundaries.md). It complements the
[job lifecycle](#lifecycle) and does not redefine its state machine.

Cache administration uses the failure and cooperative-cancellation contracts;
its CLI retains presentation and confirmation. Other CLI workflows keep their
own progress callbacks and `KeyboardInterrupt` handling. Nothing here is persisted.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.jobs.events` | Domain | Event, progress, warning, failure and outcome values, ordering rules, canonical serialization |
| `ayvu.jobs.execution` | Application | Sink and source ports, bounded in-memory log, cancellation source and token |

## Events

Each `JobEvent` has schema version `1`, an event ID, a job ID, a monotonic sequence
starting at 1, a UTC timestamp, correlation IDs and one payload. The kind follows from
the payload type:

| Kind | Payload | Meaning |
| --- | --- | --- |
| `progress` | `JobProgress` | Phase, unit, completed amount and total, or `null` when the total is not known yet |
| `warning` | `JobWarning` | A bounded code |
| `cancellation_requested` | `CancellationRequest` | Reason code and request time |
| `cancellation_acknowledged` | `CancellationAcknowledgement` | Reason code and the safe point that observed it |
| `outcome` | `JobOutcome` | The single terminal event |

**Each job has one producer**, which assigns sequence numbers. A thread requesting
cancellation does not emit an event; the producer emits it when it observes the
request. Consumers pull with `replay(after_sequence)`. There are no callbacks, so a
failing consumer cannot disturb the producer or another consumer.

`encode_event` produces one canonical text per event: sorted keys, ASCII, no
whitespace, and fixed-width UTC timestamps. `decode_event` accepts only that exact
output and rejects unknown or missing fields, duplicate keys, `NaN` and another
schema version, which it reports distinctly as `unsupported_event_schema`.

## Ordering rules

`JobEventSequence` holds the rules for one job. A rejected event changes nothing, and
every rejection raises `JobEventOrderError` with one of these codes:

| Code | Condition |
| --- | --- |
| `foreign_job` | The event belongs to another job |
| `conflicting_duplicate` | A known event ID arrives with different canonical content |
| `event_after_terminal` | A new event follows the outcome |
| `sequence_regression`, `sequence_gap` | The sequence is not exactly the next one |
| `correlation_changed` | The project differs from the stream's project |
| `phase_regression` | Progress returns to a phase that already ended |
| `progress_regression` | Completed decreases, or a known total becomes unknown |
| `progress_unit_changed` | The unit changes within a phase |
| `duplicate_cancellation` | A second request or acknowledgement event |
| `cancellation_not_requested` | An acknowledgement or `cancelled` outcome without a request event |
| `outcome_contradicts_cancellation` | An acknowledged stream ends as anything but `cancelled` |

Replaying an event with identical content returns `DUPLICATE` and is not applied
again, including after the outcome. Canonical text is the comparison, so `1` and `1.0`
are different content. A total may start unknown, become known and grow. Timestamps
are not required to increase, because clocks move; sequence is the order.

## Outcomes and failures

A `JobOutcome` holds one of the four terminal job states and the same
`JobTerminalSummary` that the job records. `outcome.matches(job)` checks the pairing.
`failed` requires a `JobFailure` whose code is the summary's reason code, and no other
state may carry one. An expired lease is not an outcome: it remains a nonterminal
`requires_reconciliation` condition.

`JobFailure` holds a family, a code, a retryable flag and diagnostics. Families are
`input`, `capability`, `policy`, `provider_transient`, `provider_permanent`,
`resource`, `integrity`, `cancelled` and `internal`. A `failed` outcome cannot use
`cancelled`. Adapters reduce failures to this envelope and raise `JobOperationError`.
`normalize_failure` never reads the message of an unknown exception, which becomes
`internal`/`unexpected_error`, because such a message may quote the book or a URL.

## Cancellation

`CancellationSource` receives requests and `CancellationToken` is the operation's only
view. The token has one method, `checkpoint(safe_point)`, so a request is visible at
safe points and nowhere else. An operation declares its safe points up front, and any
other name raises `undeclared_safe_point`, even with no request pending.

A request is idempotent: the first reason and time stand. Cancellation is cooperative
and takes effect only at the next safe point. A request cannot interrupt a blocking
provider call, and no interface may report the job as cancelled before the outcome.

`checkpoint` returns an acknowledgement once a request exists. From then on the
operation has committed to stopping and must end as `cancelled`.

### Races

One lock serializes requests, acknowledgement and `settle(outcome)`, so whichever
arrives first is the fact that the other observes:

| First | Then | Result |
| --- | --- | --- |
| Settlement | Request | `too_late`: nothing is recorded and the outcome stands |
| Settlement | Safe point | No acknowledgement |
| Settlement | Second settlement | The first outcome is returned with `first=False` |
| Request | Settlement with no safe point in between | The real outcome stands; the request is never acknowledged |
| Request and acknowledgement | Settlement other than `cancelled` | `outcome_contradicts_cancellation`, with nothing recorded |

Only the caller receiving `first=True` emits the terminal event, so a job cannot get
two. Settle before moving the job to its terminal state, and move it by compare-and-swap.

### Route to `cancelled` by job state

These routes follow the existing transition table, which this contract does not change:

| State | Route |
| --- | --- |
| `queued` | `cancelled` |
| `preflight` | Pending until a safe point in `running`, then as `running` |
| `running` | `cancelling`, `cancelled` |
| `cancelling` | `cancelled` |
| `pausing` | `paused`, `queued`, `cancelled` |
| `paused` | `queued`, `cancelled` |
| `completed`, `validating` | None: the request has no effect and validation decides the outcome |
| Terminal states | None: the request is `too_late` |

## Privacy and limits

Codes come from code, never from documents or user input. A code is a slug, which
excludes spaces, paths, URLs and sentences; the slug format cannot stop a programmer
from passing a book title that happens to be a slug, so that remains a review rule.
Diagnostics are the only free-form metadata, and their values can only be numbers or
booleans. Default `repr` output shows no diagnostics.

| Limit | Value |
| --- | --- |
| Diagnostic keys | The `DIAGNOSTIC_KEYS` allowlist |
| Diagnostics per payload | 16, at most 1 KiB when serialized |
| Numbers | Finite, non-negative, below 2^63, and not negative zero |
| Serialized event | 16 KiB |
| In-memory log | 16384 events by default; a full log raises `event_log_full` and drops nothing |

## Out of scope

Request and result types for individual use cases, an event store, a scheduler or
worker, CLI migration, Desktop signals and hard thread termination belong to other
issues. The schema has no persisted form yet, so version `1` is the format a future
store has to read.

## Enforced contracts

- [test_job](../../../tests/jobs/test_job.py).
- [test_job_store_lifecycle](../../../tests/jobs/test_job_store_lifecycle.py).
- [test_job_store_leases](../../../tests/jobs/test_job_store_leases.py).
- [test_job_store_integrity](../../../tests/jobs/test_job_store_integrity.py).
- [test_job_events_values](../../../tests/jobs/test_job_events_values.py).
- [test_job_events_sequence](../../../tests/jobs/test_job_events_sequence.py).
- [test_job_events_codec](../../../tests/jobs/test_job_events_codec.py).
- [test_job_event_log](../../../tests/jobs/test_job_event_log.py).
- [test_job_cancellation](../../../tests/jobs/test_job_cancellation.py).
- [test_job_operation_execution](../../../tests/jobs/test_job_operation_execution.py).
