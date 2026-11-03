# Job events, outcomes and cooperative cancellation

This contract implements [#126](https://github.com/MichaelAlexandreDev/ayvu/issues/126)
under the shared application contracts of `DEC-017`, recorded in the
[modular-monolith ADR](modular-monolith-boundaries.md). It complements the
[job lifecycle](job-lifecycle.md) and does not redefine its state machine.

No interface uses these contracts yet. The CLI keeps its progress callbacks and
`KeyboardInterrupt` handling, and nothing here is persisted. Later use cases adopt
them one at a time.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.job_events` | Domain | Event, progress, warning, failure and outcome values, ordering rules, canonical serialization |
| `ayvu.job_execution` | Application | Sink and source ports, bounded in-memory log, cancellation source and token |

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
