---
name: ayvu-validate
description: Choose and run the validation an Ayvu change needs (fast gate, full suite in both modes, real CLI comparisons, Docker stage) and report the results honestly. Use before declaring a change done, before a commit or push, and when filling a PR's validation section.
---

# Validate an Ayvu Change

Match the checks to the change, run them in order, and stop at the first real failure.

## Ladder

1. **Fast gate, always:** `scripts/check-fast.sh`. Import contracts, size ratchet, architecture tests and
   `git diff --check`, in about 20 seconds.
2. **Full suite, for any code or test change:** `scripts/check.sh`. It runs the suite plainly and with
   `GITHUB_ACTIONS=true` (some tests behave differently under CI), plus the lock and whitespace checks.
3. **New tests prove something.** Show that each new test fails without the change (stash or revert the
   source, run the test, restore), then passes with it. Report the tests that only lock behaviour that
   already worked.
4. **Real CLI comparisons, when `src/` changes behaviour a user can see:** run the same CLI scenarios on
   `main` and on the branch, with and without a model label when the runtime matters, and compare exit
   codes, output, files, cache rows and requests. Every difference is either intended (list it in the PR)
   or a bug.
5. **Docker stage, for behavioural changes:** `scripts/docker-tests.sh <target>` for each target (ubuntu
   with system Python, 3.11 and 3.14, debian, fedora, arch, constrained). Run the targets **one at a
   time**: parallel runs add load-only failures. Rerun a failed target alone before calling it a flake,
   and investigate any failure that repeats.
6. **Docs only:** `git diff --check` and the documentation tests (part of the fast gate).

## Rules

- Never modify a worktree while a suite, comparison or Docker run reads it; use a separate worktree.
- Intermittent failures are evidence. Reproduce under the same conditions (for example, repeat the
  test file many times in Docker, or instrument the code in a throwaway worktree that is never
  committed) before blaming load.
- A green suite does not prove completeness: check the acceptance criteria one by one.

## Report

State each check and its result with numbers (passed, skipped, failed), each intended difference, and
anything skipped and why. Never claim a check that did not run.
