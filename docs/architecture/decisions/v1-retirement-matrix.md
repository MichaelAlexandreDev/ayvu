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
| `resume` and the resume offer | = through the durable chain for rich jobs. Step 6a: a v1 job or old standalone checkpoint is refused, unchanged, before any request;  and a rich job resumes with the provider and model it started with, recovered from its provenance (AI-03, #377), and refuses an Ollama model whose weights changed | `test_cli_rich_runtime_execution.py`, `test_cli_rich_failure_matrix.py`, `test_cli_resume_runtime.py` |
| Old v1 data (step 5) | A new translation names an old v1 cache with texts for its languages, read-only, and says that it is not reused; a v1 resume says that it can be finished only until v1 is removed | `test_cli_legacy_data_notice.py` |
| Review export and apply | = through the rich review package | `test_cli_rich_runtime_execution.py` |
| `extract` | = (no translation runtime) | existing `extract` tests |
| Terminal and Markdown reports | =, plus the workers line of the plan | `test_cli_rich_dry_run.py`, `test_cli_rich_surfaces.py` |

## What stays open

- **Step 4 has shipped:** new translations require a model label and use the rich runtime.
  G1 refuses unlabeled work before any provider call or file; existing v1 work may still resume.
  Step 4f records the broader exit comparisons.
- **Step 5 has shipped:** a new translation names an old v1 cache that has texts for its languages and
  says that it is kept and not reused.
- **Step 6a has shipped:** v1 no longer runs. A v1 job or an old standalone checkpoint is refused on
  resume and left unchanged; the frozen release `v1-runtime-final` finishes it. Step 6b deletes the
  unused v1 code.

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
  *Update (2026-11-18, AI-03 #377):* a rich job now resumes with the provider and model it started
  with, whatever the configuration says, as the AI-03 issue asks.
- **Question P2:** guided translation and preview without a label ask for the label in place, with
  the same validation as the settings option, save it and continue; an empty answer cancels.
- **Question P3:** commands that do not translate do not change in step 4. These are cache
  administration, applying a review, `inspect` and `extract`; their v1 parts leave in steps 5 and 6.

Every other surface keeps the behaviour in the matrix above.

## Step 5 acceptance

On 2026-11-17 the owner accepted the step 5 design (translated quotation: "Q1-Q4 recommended"). Old
data takes path B, so step 5 is a notice and a migration guide, not an importer:

- **Q1:** a new translation (one book, guided, dry run or cache-only) shows the notice whenever the
  v1 cache at its cache path has texts for its languages; a batch shows it once, before its books.
  Continuations, old standalone checkpoints and previews do not show it.
- **Q2:** the notice is in English, like the rest of the new interface.
- **Q3:** before step 6 removes v1, the last `main` commit that still has it is tagged
  `v1-runtime-final`, with a GitHub release that says what it is for. Old jobs can be finished there.
- **Q4:** no archive importer (path A).

The v1 cache is counted read-only and never created, migrated or written; an unreadable one is
reported and the translation goes on. The guide is
[Upgrading from the v1 runtime](../../guide/upgrading-from-v1.md).

## Step 6 acceptance

On 2026-11-18 the owner accepted the step 6 design (translated quotation: "you may start"), with its
recommendations:

- **Q1:** a v1 job or an old standalone checkpoint is refused on resume, with or without a label, before
  any request, job or file, and its files stay byte for byte. The guided offer lists it without offering
  to continue. Checkpoints are not converted into new translation requests.
- **Q2:** JSON-v1 cache administration and CSV-v1 review inspection stay; they do not depend on `v1/`.
- **Q3:** `--tm-legacy-cache-compatibility` stays; it belongs to the rich runtime.
- **Q4:** the tag `v1-runtime-final`, on the last `main` commit that runs v1, has a GitHub pre-release
  that says what it is for and that it receives no fixes.

Step 6 ships in two pull requests: 6a refuses v1 work (behavioural), and 6b deletes the code that this
leaves unused (mechanical).

Step 6b deleted `src/ayvu/v1/`, `tests/v1/`, the legacy runtime selection in composition, the `legacy_resume`
chain and the fuzzy `TranslationMemory`, with the `v1` layer, its import contract and its hook rules. What stays
is listed under Q2 and Q3, plus `LEGACY_SCHEMA`, the reading of old checkpoints and every refusal. Tests that used
v1 as a vehicle now write v1 files with `LEGACY_SCHEMA`, or run the use cases on a test runtime over the real engine.

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
