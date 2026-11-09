# Recovering interrupted translations

Related issue: [#146 — Migrate interrupted-job recovery to application use cases](https://github.com/MichaelAlexandreDev/ayvu/issues/146)

Three use cases answer what was interrupted, what resuming it would mean, and resume it, for any consumer. The `resume`
command and the guided offer run through them, with the prompts they always had.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.job_recovery` | Application | The requests and results, the rules, and the three use cases |
| `ayvu.job_recovery_store` | Infrastructure | Everything those rules read, without writing |
| `ayvu.legacy_resume` | Infrastructure | Bounded reading of `*.ayvu-state.json` |

`open_job_recovery` in `ayvu.translate_project_composition` puts them together.

## What a resume is today

A full re-execution over the v1 cache, under a new job linked to the old one. It is not a selective replay, because in
production there is nothing to replay:

- The v1 cache holds languages, a hash and two texts. Nothing says which provider, model or pipeline produced a text.
- The lineage of a run records `unknown` for its producer, until [#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153).
- A run produces one artifact, `translated_output`. No raw results exist, because the rich path does not run in production.
- The plan's fingerprint holds a digest of translator and address, and no model, route, capabilities or versions.

So a cache hit avoids a call, and proves nothing. A result says so: `provider_calls_may_repeat` is true, and the reasons
include `provenance_unproven`.

## The three use cases

**`ListInterruptedJobs`** reads what is recorded and hashes no book. **`InspectJobRecoverability`** also reads the source
and the output through, which can take a while, and it can be cancelled between chunks. Both are strictly read-only: no
network, no transition, no lease, no migration, no lock taken for writing, and nothing created. A folder that does not
exist stays that way.

**`RequestJobResume`** repeats the inspection, refuses what it must, and hands the book to a `ResumeRunner`. The durable
path gives `TranslateProject` a `ContinuationTicket`, and the legacy path is the current flow.

| Compatibility | Means | Resuming |
| --- | --- | --- |
| `durable` | The file recomputes to a job's fingerprint | A new job linked to that one |
| `legacy_only` | Anything else with a readable file | The current flow, with no job |
| `job_only` | A job with no file that recomputes to it | Refused: nothing records the address, the cache or the output |
| `unsupported` | A file or a project that cannot be read | Refused |

Each entry carries reasons from one closed list, and references that say nothing readable. Results hold no path, text or
address. `legacy_entry` gives a presentation the file behind a reference, deliberately.

## From a file to a job

A file is a record of how a run was invoked, and it is never proof. It has to recompute to what the job holds:

1. The configuration digests are recomputed from the file, with the same function a translation uses, and compared with
   each project's manifest. This reads no book. A second project matching is `incompatible_plan`.
2. The plan is recomputed twice, with overwrite off and on, because the file records the flag and the run may have
   accepted a prompt instead.
3. The newest job at the end of a chain whose fingerprint is one of those two is the one.

A project with no job on that plan stays `legacy_only`. No project is created and no identity is made up to fit an old file.

**A file that does not match loses the link and not the translation.** It runs on the current flow, as it does today, and
the old job is settled by the next run of its plan or once its lease expires.

New files record `profile_name`, the only part of the identity they lacked. An old file has none, so a run that used a
profile stays `legacy_only`.

## Resuming a durable job

`TranslateProject.execute(command, continuation=ticket)` does this, holding the guards:

1. **The guards.** First the parent's plan, then its own, and only once if they are the same, since a second take in one
   run would refuse itself. A resume differs from its parent in the overwrite flag, so its own guard says nothing about
   the parent's process. A plan held by a live process is refused as `job_in_progress`.
2. **The ticket.** A revision that differs is `stale_revision`, and a job already continued is `continuation_exists`.
3. **The parent's outcome.** It is settled as in [the use case](translate-project.md#crashes-and-interruptions), and a job
   that is still not terminal is `job_not_resumable`. A job that ended stays exactly as it ended.
4. **The new job** is linked with `create_continuation`, which a unique constraint allows once per job.

Repeating a request after the first went through runs nothing: the chain answers, and the result names its last job. The
answer comes from the chain and not from the file, because the file may have finished by then. No new table or column
was needed.

A job left `queued` with no lease is settled under the guard of its plan. Its creator takes the lease under that same
guard, so it died in between, and no lease would ever expire for it. Without the guard it stays as it is, because its
creator may be between those two steps.

`paused` and `pausing` are `job_state_not_resumable`. Nothing produces them yet.

## When a run is alive

A lease cannot tell, so a reader probes [the run guard](translate-project.md#the-run-guard) with `held`, which opens the
lock file read-only and takes a shared lock for an instant. It creates and writes nothing. A run starting in that instant
waits 50 ms for its guard instead of being refused.

## What is refused, and where

| Found | `durable` | `legacy_only` |
| --- | --- | --- |
| Source changed | Refused | Refused, if a project recorded this path. Otherwise it cannot be known |
| Source gone or unreadable | Refused | Reported, and the current flow says it in its own words |
| Refused by policy | Refused before the parent is touched | Reported, and the current flow's `check_book` refuses it |
| A run alive | Refused | Not known, as today |
| Output damaged, or a file with no record | Reported, and the person decides | Reported, and the person decides |

Policy is `check_book`, the same check every other path runs, passed in by the composition root.

A person has to accept `full_reexecution`, and also `replace_destination` if a file is at the destination. The request
lists what it accepted, and is refused with `decision_required` otherwise.

## Whether a stage can be reused

`select_resume_stage` is pure. A stage is reusable only if its artifact is verified, owned, schema-valid and every
dimension in `REUSE_DIMENSIONS` was recorded and is equal. One that differs is `incompatible_plan` and is named. One
nobody recorded is `provenance_unproven`, and it is never called a change. The earliest stage that fails is where work
would start.

In production the answer is always `translate`, because no raw artifact exists. **Nothing executes a selection yet.**
Replaying raw results after a restart needs #153 for the evidence, and also a new operation: an expired lease cannot be
released, renewed or acquired, a continuation does not own its parent's artifact, and reconciliation ends a `running` job
before anything could reuse it.

## What the CLI does with them

It lists through `ListInterruptedJobs` and shows the files behind the entries, so a job with no file is not shown. Asking
for a resume is the decision, as it always was, so it accepts `full_reexecution` and `replace_destination` by itself, and
the sentence it prints about cached translations and retried segments is what discloses that calls can repeat.

A refusal appears after the confirmation, followed by the same closing line as any failed resume.

| What changed for someone resuming | Before | Now |
| --- | --- | --- |
| The original process is still alive | Both ran, and both wrote the output | Refused, as a duplicate `translate` is |
| The book changed, and a project recorded it | Translated the new book in silence | Refused, as `translate` does |
| The book is gone, on a durable job | "Não foi possível ler o EPUB" after the plan | "Informe uma fonte existente", with no plan |
| A book refused by policy, on a durable job | Refused after the plan | Refused, with no plan |
| A provider failure under `--fail-fast` | A traceback | The same message `translate` gives |
| A state file that is not UTF-8, or a directory | A traceback | Listed as invalid |
| A computer asleep for more than an hour | Carried on | Ends as `lease_expired`, as `translate` does |

A translation that resumes through a job has the output of `translate`: it is published atomically, and an old file stays
intact until the end. The job left behind now gets its outcome and a link, where before it stayed `running` after the
book was done.

## Old files

- **Reading** is bounded to 8 MiB, regular files only, without following a link. A bad file is one `unsupported` entry
  and stops nothing, including a file that is not UTF-8.
- **Reading never changes a file.** Listing, inspecting and being refused leave its bytes and its time as they were.
- **A confirmed run on the current flow rewrites it**, as it does today: the file is how that flow records its own
  progress, and marking it `completed` is what stops it being offered again.
- A new run of the same book and target also replaces it, because the name holds only those two. Two books with one
  name share one file, and the first remains as a `job_only` entry.

## What this does not prove

- What produced any text in the cache, until #153.
- Whether a call interrupted by a crash reached the provider. It is made again.
- That a `legacy_only` book is the one it was, when no project recorded it.
- Native Windows and network filesystems, as for the guard itself.
