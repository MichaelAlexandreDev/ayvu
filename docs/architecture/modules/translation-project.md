# Project translation

The runtime restrictions in the #252 integration below describe that integration's
scope. They are preserved alongside the later context and retrieval contracts.

Related issue: [#145 — Extract the translate-project application use case](https://github.com/MichaelAlexandreDev/ayvu/issues/145)

`TranslateProject` translates one book of one stored project as one durable job, for any consumer. It imports no
framework, store, format or provider: it receives them as ports from a composition root.

`translate` and the guided translation run through it, one book at a time, and so does each book of a batch, and a
[resume](recovery.md) of a job it left behind. Dry run, preview and a resume file that recomputes to no job have no
durable project execution. Unlabeled previews and dry runs keep the predecessor path behind
`translate_without_project`; labeled dry runs use the [offline transient route](translation.md#offline-transient-dry-runs-ref-18-g3),
and labeled previews use the [online transient route](translation.md#online-transient-previews-ref-18-g2).

## Explicit runtime selection (#252)

`ayvu.composition.cli` resolves `translator_models` for the selected translator and
`ayvu.composition.builders.translate_project` constructs `LegacyTranslationRuntime` or
`RichTranslationRuntime`. Without a label, legacy composition and fingerprint remain
the same. A valid label (`[a-z0-9][a-z0-9_-]{0,47}`) produces model
`declared:<label>`; `unknown` has not been made eligible. **Changing the server model
without updating the label reuses old results under the same declaration.**

Online rich execution prepares `SQLiteExactCache` at `<--cache name>.v2.sqlite` before
provider preparation. Durable cache-only instead runs the explicit local preflight and opens
`ReadOnlyExactCache` before any online callback. It uses
[`rich_document_executor.py`](../../../src/ayvu/composition/builders/rich_document_executor.py) to
wire [`CacheOnlyDocumentExecutor`](../../../src/ayvu/translation/cache_only_execution.py), with
normal project/job/lease authority and per-unit exact reuse provenance. Neither branch opens
or migrates v1. Rich durable execution still requires `--workers 1`; full-cache requires cache-only.
The engine's coverage gate precedes reconstruction/staging/publication. Gaps under full-cache
end `PARTIALLY_COMPLETED/cache_incomplete`; the existing report and missing-text renderer still run.
No cache-only path opens TM, fuzzy, replay or provider ports, or publishes raw-results.
Internal pins can only be resolved by an explicitly injected durable authority; the production CLI
uses compatible resolution and exposes no pin flag.

An application consumer may inject `ProjectTranslation.translation_context`
with explicit snapshots and authorization. The [bounded context policy](translation-context.md)
prepares and validates disclosure before repositories/provider, revalidates at each
call's egress and includes the effect in the job fingerprint only when declared.
This does not create implicit authorization from a profile or CLI configuration.

The port receives a `RuntimeContext` with a real job, cancellation and event channel.
Before each document, composition queries the current job to observe renewed
leases, keeping the original owner's token to avoid adopting another process's ownership.
`TranslationResources.prepare_document` prepares a document's executions before
the engine consumes its units. Only composition knows `DocumentTranslator` and its
repositories. Preparation exceptions stop the book; the engine and use case
do not select runtime. The engine retains chapter progress; internal document
progress events are not interleaved with that sequence. Warnings are forwarded,
and cancellation is emitted once at closure.

The command carries generic execution identity: revision `1` in legacy and `2` in
rich, optional producer and additional output roles. The revision is recorded in
`JobRequest.command_version`, with `translated_output` and `raw_results` in `output_roles`
in rich; `execution_identity` provenance and result records store the
declared producer. The job fingerprint includes these dimensions only when declared.
Continuation compares revisions before creating the new job and refuses `runtime_mismatch`;
the CLI names `legacy/v1` and `rich/v2`. The rich checkpoint stores only execution
revision `2`; the label is not copied. Recovery recomputes the fingerprint with the producer
already recorded in the job, regardless of current configuration. Without proof, it refuses
association rather than downgrading the entry to v1. Legacy checkpoints omit the new field.

A provider failure in rich leaves a resumable `failed` job, without a partial EPUB.
Already completed segments remain in v2; retry uses these pre-glossary values.
The runtime applies the glossary on each run, including on cache hits.
Raw results are produced per document. A continuation receives the previous job chain
in `RuntimeContext.reusable_jobs` and reuses only what it proves equal (#242,
[recovery](recovery.md#replay-through-a-continuation-242)). Publication, validation, reporting and review export follow
the same ports as the legacy path, preserving the original EPUB.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.translation.project` | Application | Admission, exclusion, reconciliation and provenance for the project use case |
| `ayvu.translation.project_contracts` | Application | The command and result, the ports and the execution plan fingerprint |
| `ayvu.translation.project_run` | Application | One active job from its acquired lease to its terminal outcome |
| `ayvu.translation.project_lease` | Application | Concurrent renewal of the active job lease |
| `ayvu.translation.engine` | Application | One book through a format adapter and a unit executor |
| `ayvu.artifacts.destination` | Infrastructure | Publishing to the file the user chose, and the way back to it after a crash |
| `ayvu.composition.builders.translate_project` | Composition | The current language work and validator behind the ports, and the assembled use case |

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
| A legacy text failed and stayed in the source language | `partially_completed` | `texts_failed` | Published, as today |
| Legacy cache-only, with gaps | `partially_completed` | `texts_missing` | Published, as today |
| Legacy `--require-full-cache`, with gaps | `partially_completed` | `cache_incomplete` | None, as today |
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

A run waits 50 ms for its guard, so that a reader [probing it](recovery.md#when-a-run-is-alive) cannot make it refuse.
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
- **It receives producer identity from composition.** Legacy lineage keeps `unknown` because v1 cannot identify its
  producer. Rich runs record the LibreTranslate adapter and the user-declared model; they do not probe the server.
- **It does not decide what to resume.** A job that ended stays ended, and `execute` only links a new job to it when
  given a ticket. [Job recovery](recovery.md) decides. The resume file is still kept by the CLI, chapter by chapter,
  around the use case, because it is the only record of the address, the cache and the output.
- **It does not write the review package itself.** `translate --review-output`
  passes the exact result to `ExportReviewPackage`; the shared review use case
  validates identity and publishes the v2 package through D106.

## What the CLI adds

`ayvu.interfaces.cli.app` turns each failure code into a message in its own table, and shows the exception's text only in developer
mode. A book the adapter refuses by policy (protected content, unsafe names, duplicates, entities, the size and
compression limits) is refused by `check_book` on every path, including dry run, preview and `resume`, before any
translator is asked and before a resume file is written. A book the adapter merely cannot read is still judged by the
current preflight, with its current message.

A provider's empty `translatedText` stays a success on the legacy path. Rich runs refuse it as `empty_result` and
publish no partial EPUB.

## Enforced contracts

- Admission, ports and diagnostics: [plan gates](../../../tests/translation/test_translate_project_plan_gates.py),
  [framework ports](../../../tests/translation/test_translate_project_framework_ports.py), and
  [diagnostics](../../../tests/translation/test_translate_project_diagnostics.py).
- Publication, continuation and run ownership: [destination](../../../tests/translation/test_translate_project_destination.py),
  [continuation](../../../tests/translation/test_translate_project_continuation.py),
  [cancellation](../../../tests/translation/test_translate_project_cancellation.py),
  [lease renewal](../../../tests/translation/test_translate_project_lease_renewal.py), and
  [reconciliation](../../../tests/translation/test_translate_project_reconciliation.py).
- Explicit runtime integration: [selection](../../../tests/composition/test_rich_runtime_selection.py),
  [preparation](../../../tests/composition/test_rich_runtime_preparation.py),
  [execution](../../../tests/composition/test_rich_runtime_execution.py), and
  [recovery](../../../tests/composition/test_rich_runtime_recovery.py).
- Sequential batches and accepted outputs: [batch units](../../../tests/translation/test_translate_batch_unit.py),
  [project contract](../../../tests/translation/test_translate_batch_project_contract.py),
  [framework integration](../../../tests/translation/test_translate_batch_framework_integration.py),
  [composition](../../../tests/composition/test_translate_batch_composition.py),
  [output policy](../../../tests/translation/test_batch_output_policy.py), and
  [publication races](../../../tests/translation/test_batch_publication_race.py).
