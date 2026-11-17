# Compatibility matrix of the v1 retirement

Status: Accepted
Decision-Date: `2026-11-12`
Owners: the Ayvu repository owner and maintainer (inventory, compatibility and data-path acceptance); Composition and Translation (runtime selection and execution); Knowledge (cache and memory); Recovery (checkpoints and continuation); Formats (HTML/EPUB); Review (review exchange).
Issue: [#286 — REF-18](https://github.com/MichaelAlexandreDev/ayvu/issues/286).

Companion of the [v1 retirement decision](v1-retirement.md). It closes step 3 of its
[ordered implementation](v1-retirement.md#ordered-implementation-after-approval): every user surface,
what it does without a model label (v1, the reference) and with one (the rich runtime), and the CLI
tests that hold the difference in place. Written on 2026-11-16, when step 3 closed. The table records the step 3 baseline; step 4 admission is now implemented as recorded below.

## Legend

- **=**: the same observable behaviour.
- **A(n)**: an intentional difference accepted by answer *n* of the
  [acceptance record](v1-retirement.md#acceptance-record) (2026-11-12).
- **A(s2-n)**: accepted by answer *n* of the [step 2 acceptance](v1-retirement.md#step-2-acceptance) (2026-11-13).
- **A(s3-A)**: accepted by the owner's answer to step 3 design question A (2026-11-16; see the end of this record).

## Matrix

All test paths are under `tests/interfaces/cli/`.

| Surface | With a label, compared with v1 | CLI tests with a label |
| --- | --- | --- |
| Setting the label | G1: new translations without a model label are refused before provider calls or files. Guided menus 1 and 2 ask in place, save and continue; Enter cancels. Settings option 5 uses the same validation; nothing is invented or copied from the server. | `guided/test_guided_model_label.py`, `guided/test_guided_translation_model_label.py`, `test_cli_model_label_gate.py` |
| `translate`, one book | A(7): no partial EPUB; raw results and review package | `test_cli_rich_runtime_execution.py` |
| Guided translation (menu 1) | = through the durable route (job revision 2, `declared:<label>` producer) | `test_cli_rich_surfaces.py` |
| Batch (several EPUBs) | = durable per book. A failed book publishes no partial EPUB; by default and with `--fail-fast` the batch stops after it, and `--continue-on-error` goes on. | `test_cli_rich_surfaces.py`, `test_cli_rich_missing_output.py` |
| `--preview`, root and guided | A(s2-1) | `test_cli_rich_preview.py` |
| `--dry-run` | A(s2-1), A(s2-3), A(s2-4) | `test_cli_rich_dry_run.py` |
| `--cache-only`, `--require-full-cache`, `--missing-output` | A(s2-2), A(s2-3) | `test_cli_rich_cache_only.py`, `test_cli_rich_missing_output.py` |
| `--workers` and performance profiles | A(s2-5), A(s2-6) | `test_cli_rich_workers.py`, `test_cli_rich_cache_only_workers.py` |
| Translation memory flags | G9, A(7): governed approved memory, fuzzy is a suggestion unless `--tm-conservative-auto-apply` at 0.98 or more; the v1 cache is not memory. A(s3-A): the CLI says so, and warns when `--tm-apply-threshold` is given without the conservative flag. | `test_cli_rich_runtime_policy.py`, `test_cli_rich_memory_notices.py` |
| Provider failure | G10, A(7): the book always stops without a partial EPUB, whatever `--fail-fast` and the worker count say, and resumes from its completed documents | `test_cli_rich_failure_matrix.py` |
| `resume` and the resume offer | = through the durable chain; a job resumes on the runtime it started with, and a rich job resumed with another label translates again with `raw_results_changed_provider` | `test_cli_rich_runtime_execution.py`, `test_cli_rich_failure_matrix.py`, `test_cli_resume_runtime.py` |
| Review export and apply | = through the rich review package | `test_cli_rich_runtime_execution.py` |
| `extract` | = (no translation runtime) | existing `extract` tests |
| Terminal and Markdown reports | =, plus the workers line of the plan | `test_cli_rich_dry_run.py`, `test_cli_rich_surfaces.py` |

## What stays open

- **Step 4 has shipped:** new translations require a model label and use the rich runtime.
  G1 refuses unlabeled work before any provider call or file; existing v1 work may still resume.
  Step 4f records the broader exit comparisons; old-data notices and removal remain steps 5 and 6.

The owner answered the step 3 design questions on 2026-11-16 (translated quotation: "A. The
recommended one. B the recommended one. C the recommended one"):

- **A:** an explicit `--tm-apply-threshold` without `--tm-conservative-auto-apply` warns and the run
  continues, as already shipped.
- **B:** without a label, `--cache-only` with more than one worker is refused clearly instead of
  crashing (#365); v1 never supported it.
- **C:** no separate command sets the model label; guided settings and `config.json` cover it.

## Step 4 acceptance

On 2026-11-17 the owner accepted the step 4 design (translated quotation: "I accept"), which makes
the rich runtime the default:

- **Without a model label, no new translation runs.** `translate` (one book or a batch), guided
  translation, `--preview`, `--dry-run` and `--cache-only` are refused before any provider call,
  project, cache or file, with a message that says how to declare the label. Ayvu never invents a
  label and never falls back to v1 silently (answer 4).
- **The v1 cache does not speed up the first labeled translation:** texts are translated again into
  the v2 cache, as answer B of 2026-11-12 already accepted. The notice about old data is step 5.
- **Question P1:** a resume uses the runtime its job started with. A v1 job finishes on v1, with or
  without a label, until step 6 removes v1; a rich job needs a label. This only finishes existing
  work: no new translation reaches v1. *Correction (2026-11-17):* the design also said that a rich
  job needs the label it started with. That described current behaviour wrongly: a rich job resumed
  with another label already translates its documents again and warns
  (`raw_results_changed_provider`), as the README documents. The answer to P1 did not change that.
- **Question P2:** guided translation and preview without a label ask for the label in place, with
  the same validation as the settings option, save it and continue; an empty answer cancels.
- **Question P3:** commands that do not translate do not change in step 4. These are cache
  administration, applying a review, `inspect` and `extract`; their v1 parts leave in steps 5 and 6.

Every other surface keeps the behaviour in the matrix above.

## Step 4 exit proof (2026-11-17)

These are the runs that the [ordered plan](v1-retirement.md#ordered-implementation-after-approval) asks for. They used the real CLI with isolated user folders and a fake loopback LibreTranslate server. Each run compared `main` before the switch (`5cb6025`) with `main` after it (`274c77c`, #389), on the maintainer's Arch Linux machine.

- **With a configured label:**
  - **42 translation and resume scenarios:** all identical.
  - **71 CLI scenarios:** 62 identical; 8 differ only in the width of the Rich tables.
    - The width comes from the length of the run folder's name.
    - The remaining scenario passes an unknown `--translator` together with a non-loopback URL. Before the switch, Ayvu created a project and then failed on the network policy. Now it refuses at once with the unknown-translator error and creates nothing.
  - **End-to-end interruption and resume run:** no differences.
- **Without a label:**
  - **71 CLI scenarios:** 53 change, and each change is the refusal, at a step that translates. That step is `translate` (42 steps), root `--preview` (3) or guided option 2, the default (30 steps). 7 scenarios are identical; the other 11 differ only in table width or create nothing, and one of these fails in its own harness on both sides.
  - The resume scenarios cannot set up an unlabeled job any more. The suite covers unlabeled v1 resume: `test_cli_resume_runtime.py`.
- **After the switch:** an end-to-end run against a real loopback HTTP server passed all 16 checks:
  - without a label, `translate`, `--dry-run`, `--cache-only` and root `--preview` exit 1 with the refusal, send no HTTP request and create no file;
  - with a label:
    - the default run translates every text once;
    - a warm repeat sends nothing and publishes the same EPUB;
    - warm cache-only opens no session and publishes the same EPUB;
    - cold cache-only with `--require-full-cache` exits 1 without an EPUB; without that flag it keeps the source text and writes the missing-text file;
    - dry run publishes nothing, and preview publishes a preview;
    - an interrupted run resumes with only the missing texts and matches a clean run byte for byte;
    - review export and `apply-review` put the reviewed text in the EPUB;
    - common mode saves the Markdown report.
