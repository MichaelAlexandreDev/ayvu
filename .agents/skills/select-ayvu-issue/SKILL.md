---
name: select-ayvu-issue
description: Select and assess the next implementation-ready Ayvu GitHub issue from the architectural backlog. Use when prioritizing open issues, checking readiness or dependencies, or deciding what can safely start next. Do not use to draft new issues or implement changes.
---

# Select Ayvu Issue

Recommend one published issue that can begin without bypassing an architectural
gate. Keep the entire workflow read-only.

## Sources of truth

1. Read AGENTS.md and the architecture guide.
2. Read the decision records that gate the candidate issues
   (`docs/architecture/decisions/`), including their `Status`: a `Proposed`
   record does not satisfy a dependency.
3. Query the live GitHub issue before trusting a local tracker or old session.
4. Treat code, tests, pyproject.toml, uv.lock, user documentation, and artifacts
   present in main as evidence of current implementation.

Do not expose, copy, edit, or publish private files while selecting an issue.

## Readiness audit

1. Capture the current branch, HEAD, main, origin/main, remotes, and complete
   worktree status without changing them.
2. List all open issues and the ten most recently closed issues. Inspect the
   complete body and metadata of each serious candidate.
3. Build a dependency view from explicit hard dependencies. An open issue,
   local draft, passing validator, or unmerged branch does not satisfy a
   dependency.
4. Confirm each required artifact in main by inspecting the real file, symbol,
   schema, test, ADR status, or documented decision.
5. Exclude candidates that are Deferred, depend on an unaccepted Proposed
   decision, have missing artifacts, collide with a branch, are XL, or diverge
   materially from the repository.
6. Rank the remaining candidates by:
   - hard dependency order;
   - P0 before P1 before lower priorities;
   - ability to unlock downstream work;
   - smallest coherent risk and review surface.
7. Read the likely target modules, tests, and user documentation for the leading
   candidate. Do not claim readiness from titles or labels alone.

Do not fetch, create or switch branches, edit an issue, create a draft, or
modify any file.

## Result

Return:

- recommended issue number, title, priority, estimate, and suggested branch;
- why it is ready, with file or GitHub evidence;
- verified hard dependencies and the artifacts found in main;
- likely files and validation commands, clearly marked as provisional;
- blockers or unresolved divergence;
- up to two alternatives, ordered, when useful.

If no issue is ready, say so and identify the smallest concrete gate to close.
Never recommend bypassing the gate or implementing dependency-owned contracts
inside a downstream issue.
