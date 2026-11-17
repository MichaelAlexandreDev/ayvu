---
name: ayvu-ship-pr
description: Prepare and ship an Ayvu pull request (commit message, PR body, CHANGELOG, merge, cleanup) once the maintainer has authorised it. Use when a validated change is ready to commit, push, open as a PR or merge. Never use to bypass an approval.
---

# Ship an Ayvu PR

Each Git action needs the maintainer's authorisation: committing, pushing, opening a PR and merging
are separate approvals unless the maintainer granted them together in writing.

## Before committing

- `$ayvu-validate` ran, and its results are known.
- The change is mechanical **or** behavioural (D13). A ratchet-driven extraction inside a behavioural
  PR is its own commit and is explained in the PR body.
- User-visible changes have docs (`README.md`, `docs/guide/`) and a `CHANGELOG.md` entry, in Portuguese.
- No private file is staged: books, caches, `glossary.json`, `.ayvu-pessoal/`, `CLAUDE.local.md`.

## Commit message

English, `type: summary (#issue)` (types: feat, fix, refactor, test, docs, chore, ci, build), with a
body explaining why, followed by the attribution lines the session requires.

## PR body

English, short and scannable:

- the issue and the decision it implements;
- what changes, and what does not;
- intended differences, with the evidence;
- validation, with numbers: suite in both modes, comparisons, Docker per target;
- known limits.

Use `Refs #N` until every acceptance criterion is proven, then `Closes #N`.

## Merging

- Update the branch, wait for CI, and merge without `--admin`. Never force-push `main`.
- Merge stacked work in order, rebasing between merges. Conflicts in `CHANGELOG.md` or test registries
  keep both entries.

## After merging

- Delete the branch and its worktree.
- Fast-forward the main checkout, then run `uv sync` and `graphify update .`.
- Update the maintainer's tracking notes, if they keep any.
