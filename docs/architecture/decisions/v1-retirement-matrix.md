# Compatibility matrix of the v1 retirement

Status: Accepted
Decision-Date: `2026-11-12`
Owners: the Ayvu repository owner and maintainer (inventory, compatibility and data-path acceptance); Composition and Translation (runtime selection and execution); Knowledge (cache and memory); Recovery (checkpoints and continuation); Formats (HTML/EPUB); Review (review exchange).
Issue: [#286 — REF-18](https://github.com/MichaelAlexandreDev/ayvu/issues/286).

Companion of the [v1 retirement decision](v1-retirement.md). It closes step 3 of its
[ordered implementation](v1-retirement.md#ordered-implementation-after-approval): every user surface,
what it does without a model label (v1, the reference) and with one (the rich runtime), and the CLI
tests that hold the difference in place. Written on 2026-11-16, when step 3 closed. The default does
not change here; that is step 4.

## Legend

- **=**: the same observable behaviour.
- **A(n)**: an intentional difference accepted by answer *n* of the
  [acceptance record](v1-retirement.md#acceptance-record) (2026-11-12).
- **A(s2-n)**: accepted by answer *n* of the [step 2 acceptance](v1-retirement.md#step-2-acceptance) (2026-11-13).
- **R(A)**: follows the recommendation of step 3 design question A, applied while the owner's answer is pending.

## Matrix

All test paths are under `tests/interfaces/cli/`.

| Surface | With a label, compared with v1 | CLI tests with a label |
| --- | --- | --- |
| Setting the label | G1: guided settings option 5 declares, changes or removes it, validated with the saved rule; nothing is invented or copied from the server. v1 has no label. | `guided/test_guided_model_label.py` |
| `translate`, one book | A(7): no partial EPUB; raw results and review package | `test_cli_rich_runtime_execution.py` |
| Guided translation (menu 1) | = through the durable route (job revision 2, `declared:<label>` producer) | `test_cli_rich_surfaces.py` |
| Batch (several EPUBs) | = durable per book. A failed book publishes no partial EPUB; by default and with `--fail-fast` the batch stops after it, and `--continue-on-error` goes on. | `test_cli_rich_surfaces.py`, `test_cli_rich_missing_output.py` |
| `--preview`, root and guided | A(s2-1) | `test_cli_rich_preview.py` |
| `--dry-run` | A(s2-1), A(s2-3), A(s2-4) | `test_cli_rich_dry_run.py` |
| `--cache-only`, `--require-full-cache`, `--missing-output` | A(s2-2), A(s2-3) | `test_cli_rich_cache_only.py`, `test_cli_rich_missing_output.py` |
| `--workers` and performance profiles | A(s2-5), A(s2-6) | `test_cli_rich_workers.py`, `test_cli_rich_cache_only_workers.py` |
| Translation memory flags | G9, A(7): governed approved memory, fuzzy is a suggestion unless `--tm-conservative-auto-apply` at 0.98 or more; the v1 cache is not memory. R(A): the CLI says so, and warns when `--tm-apply-threshold` is given without the conservative flag. | `test_cli_rich_runtime_policy.py`, `test_cli_rich_memory_notices.py` |
| Provider failure | G10, A(7): the book always stops without a partial EPUB, whatever `--fail-fast` and the worker count say, and resumes from its completed documents | `test_cli_rich_failure_matrix.py` |
| `resume` and the resume offer | = through the durable chain; a job resumes only with the runtime and label it started with | `test_cli_rich_runtime_execution.py`, `test_cli_rich_failure_matrix.py` |
| Review export and apply | = through the rich review package | `test_cli_rich_runtime_execution.py` |
| `extract` | = (no translation runtime) | existing `extract` tests |
| Terminal and Markdown reports | =, plus the workers line of the plan | `test_cli_rich_dry_run.py`, `test_cli_rich_surfaces.py` |

## What stays open

- **Step 4** switches the default: without a label, translation is refused clearly before any
  provider call (answer 4), and setup offers the guided option. Until then, an unlabeled run keeps v1.
- **Design question A** (the threshold warning) is applied as recommended; the owner may still choose
  a refusal instead.
