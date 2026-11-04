# The translate-project use case

Related issue: [#145 — Extract the translate-project application use case](https://github.com/MichaelAlexandreDev/ayvu/issues/145)

`TranslateProject` translates one book of one stored project as one durable job, for any consumer. It imports no
framework, store, format or provider: it receives them as ports from a composition root.

`translate` and the guided translation run through it, one book at a time, and so does each book of a batch. Dry run,
preview and the `resume` command have no project, so they keep the current flow behind `translate_without_project`.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.translate_project` | Application | The command and result, the ports, the plan and the job from creation to its outcome |
| `ayvu.translation_engine` | Application | One book through a format adapter and a unit executor |
| `ayvu.artifact_destination` | Infrastructure | Publishing to the file the user chose, and the way back to it after a crash |
| `ayvu.translate_project_composition` | Composition | The current language work and validator behind the ports, and the assembled use case |

## What happens, in order

1. **Refusals, with no job created.** The project must load, its language pair must match, and the source on disk must
   still be the revision the project records. A job store that cannot be opened stops here too, before any provider.
2. **Reconciliation.** Whatever an earlier run left behind in this project is settled first.
3. **Duplicates.** The run takes the plan's guard. If another live process holds it, the run is refused as
   `job_in_progress`. A plan that ended earlier is new work.
4. **The job.** `queued -> preflight`, then the provider and cache checks. Their failure ends the job as `failed`.
5. **The book.** `running`. The engine extracts, translates and reconstructs.
6. **Staging.** The bytes go to a temporary file next to the destination, are read back and checked.
7. **Validation** of that file, while the destination is still untouched.
8. **Publication**, atomically. Then `completed -> validating ->` the terminal outcome.

## Outcomes

| What happened | Job state | Reason code | Output |
| --- | --- | --- | --- |
| Everything worked | `validated` | `validated` | Published |
| Validation warnings | `partially_completed` | `validation_warnings` | Published, as today |
| A text failed and stayed in the source language | `partially_completed` | `texts_failed` | Published, as today |
| Cache-only, with gaps | `partially_completed` | `texts_missing` | Published, as today |
| `--require-full-cache`, with gaps | `partially_completed` | `cache_incomplete` | None, as today |
| The lineage could not be written | `partially_completed` | `provenance_failed` | Published |
| Publishing failed after the file became visible | `partially_completed` | `publication_uncommitted` | Published, and the failure is reported |
| Provider, format, conflict or lease failure | `failed` | The failure's code | None |
| Cancelled | `cancelled` | `cancelled` | None. An old file under overwrite stays |

A failure stores its code, never its message, which may quote the book or the provider's address. The exception stays
on the result in `raised`, for the presentation that chose this book.

`validating` comes after validation has already run, because a cancellation has to be able to land after validation
and before publication, and the job has no way from `validating` to `cancelled`. The state records that the output was
checked and published. This validator is the current `validate_output_epub`, behind a port, not the adapter's own.

## Publishing to the chosen file

The output goes straight to the destination the user chose. A copy in the project would use the disk again on every
run, with no retention defined yet, and would leave the book in plain text in a second place.

The artifact protocol does not change. The record is reserved, the bytes are staged and measured, and only then do they
become visible. `LocalDestinationPayloads` differs in where:

- **The temporary file** sits next to the destination, as `.ayvu-<artifact UUID>.part`, so publishing is one atomic
  step on one filesystem and no document name appears in it.
- **A new file** uses a hard link, which fails rather than replaces, so an output that appeared meanwhile is never
  overwritten and the job ends with `output_exists`.
- **`--overwrite`** swaps the file with `os.replace` only at the end. Until then the old file is intact. It keeps the
  old file's mode, and a link at the destination stays a link.
- **Without hard links**, as on exFAT, an exclusive create takes the name and the file is swapped over it. That still
  cannot overwrite anything, but an empty file is visible for a moment. A crash in that moment leaves it there, because
  Ayvu never removes a file at the destination. This path was tested only with an injected failure of the link call.

The artifact record derives its location from its ID and holds no path, so a journal in
`projects/<UUID>/artifacts/publications/<artifact UUID>.json` records the destination, durably, before anything exists
out there. It lives only as long as the publication does. The project folder already holds the source's path in its
manifest, so this adds no new kind of data there.

## Crashes and interruptions

Reconciliation runs at the start of every execution, and through `reconcile()`.

| Left behind | Artifact | Job | Files |
| --- | --- | --- | --- |
| Crash while writing | `abandoned` | `failed`, `interrupted` | Temporary file removed |
| Crash after staging | `abandoned` | `failed`, `interrupted` | Temporary file removed, old file intact |
| Crash right after publishing | `ready` | `partially_completed`, `interrupted_after_publication` | Output kept, temporary name removed |
| Crash after the record went ready | `ready` | `partially_completed`, `interrupted_after_publication` | Output kept |

A job is settled only if its lease has expired, if it has none in a state that needs one, or if it is on this run's own
plan and the guard shows its owner is gone. Any other job with a live lease is left alone. Ctrl+C is handled in place by
the same rules, and it still reaches the caller.

A crash is judged by the bytes. If a file with exactly the expected bytes appeared at the destination by other means,
the run cannot tell it from its own, and it ends as `publication_uncommitted` and not as a conflict.

## The run guard

A lease cannot say that its process died: after a `kill`, a closed terminal or a power cut it stays live for up to an
hour, and repeating the command would be refused for that whole time. So each run also holds one lock file per plan, in
`projects/<UUID>/runs/`, and the system releases it when the process ends, however it ends.

Holding the guard therefore proves that a live lease on this plan belongs to nobody, and the run takes that job over and
settles it. A job on another plan is left to its lease, and a queued job holds nothing and waits for its lease to expire.
The guard also makes the duplicate check atomic across processes. The lock files stay, because removing one could split a
lock between two processes.

Two runs of one book on different plans, such as different outputs, can still run together.

A run waits 50 ms for its guard, so that a reader [probing it](job-recovery.md#when-a-run-is-alive) cannot make it refuse.
A job left `queued` with no lease on this plan is settled here, because its creator died between creating it and taking
the lease, under this same guard.

## Leases

A separate thread renews the lease every 5 minutes, up to the 1-hour maximum, because one chapter or one blocked call
can outlast any safe point. A run that loses its lease cannot publish, because the artifact store checks it.

**A computer asleep for more than an hour loses the lease.** That run ends as `failed`, `lease_expired`. The cache keeps
what was translated, so running it again is quick. This is new: before #145 the same run would carry on after waking up.
The run keeps translating after it wakes, and it learns of the loss only when it tries to publish.

## Cancellation

`TranslateProject.cancellation()` returns the source to pass to `execute`. Requests are seen at `translate_before_start`,
before each chapter and unit, at the adapter's own safe points, and at the artifact's. A blocking provider call is not
interrupted: it is seen at the next safe point. A request after the last safe point arrives too late, and the job ends
as it would have. A worker that commits to cancelling wins over a failure in another worker.

## What it does not do

- **Dry run and preview are refused as commands.** They need no project and create none, and identity must never be
  made up to fit them, so they stay on the current flow behind `translate_without_project`.
- **It claims nothing about the producer.** The lineage record says `unknown` for provider, model, adapter and pipeline,
  because the v1 cache cannot say which produced a text. #153 and #146 replace this.
- **It does not decide what to resume.** A job that ended stays ended, and `execute` only links a new job to it when
  given a ticket. [Job recovery](job-recovery.md) decides, and `resume` keeps working from its own file until it migrates.
  That file is still kept by the CLI, chapter by chapter, around the use case.
- **It does not write the review CSV.** The CLI writes it from the result until #149.

## What the CLI adds

`ayvu.cli` turns each failure code into a message in its own table, and shows the exception's text only in developer
mode. A book the adapter refuses by policy (protected content, unsafe names, duplicates, entities, the size and
compression limits) is refused by `check_book` on every path, including dry run, preview and `resume`, before any
translator is asked and before a resume file is written. A book the adapter merely cannot read is still judged by the
current preflight, with its current message.

A provider's empty `translatedText` stays a success on this path, as it is today, because it still runs through the
current translator. #153 settles it with the rich path.
