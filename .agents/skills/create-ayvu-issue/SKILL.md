---
name: create-ayvu-issue
description: Draft, review, normalize, and validate implementation-ready GitHub issues for Ayvu. Use when creating or rewriting one Ayvu issue or roadmap item, checking duplication and dependencies, choosing branch, priority, estimate, labels, or model, or preparing an issue for local review before any GitHub publication.
---

# Create Ayvu Issue

Create a focused issue grounded in repository evidence and suitable for one
reviewable pull request.

## Workflow

1. Separate permission to analyze, return a draft in the response, save a local
   draft, and publish. Never infer filesystem or GitHub mutation permission from
   a drafting request.
2. Read `AGENTS.md`, the architecture guide, target modules, tests, user docs, and current
   CLI help relevant to the issue.
3. Inspect all open GitHub issues and the ten most recently closed issues
   read-only for overlap and current conventions.
4. Distinguish existing CLI behavior, desktop adaptation, new enhancement,
   deferred work, and intentionally unsupported behavior.
5. Draft in English using `references/issue-architecture.md`.
6. Cite real modules, commands, options, formats, tests, and invariants. Never
   invent repository facts.
7. Add only hard, acyclic dependencies. An issue may not depend on a future local
   identifier in a sequential roadmap.
8. Use one unique branch whose type matches the actual deliverable. A docs branch
   cannot silently own production implementation.
9. Recommend one current Codex model and reasoning effort according to task risk,
   complexity, latency, and cost. Recheck the active Codex selector and official
   OpenAI documentation instead of copying stale recommendations.
10. Run `scripts/validate_issue.py` and perform the manual checklist.
11. Return the issue draft in the response by default. Save it under
    `.ayvu-pessoal/` only after explicit authorization for the exact path, and
    publish only after explicit authorization for that exact issue.

Before an authorized private save, resolve the target and its parents inside
the approved `.ayvu-pessoal` root. Refuse symlinks, hardlinked or non-regular
existing targets, and any path that escapes containment. Overwriting an
existing file needs separate approval. Use the environment's safe editing
mechanism only when it guarantees atomic publication. Stage an authorized
write in an exclusive restrictive temporary file in the same directory, flush
it, replace the target atomically, and clean up on failure; otherwise return the
draft without saving it.

## Canonical sections

Use this order:

1. Context
2. Objective
3. Scope
4. Out of scope
5. Technical requirements
6. Acceptance criteria
7. Tests required
8. Dependencies
9. Suggested branch
10. Priority
11. Estimate
12. Recommended model
13. Labels

## Rejection rules

Reject or revise an issue when it duplicates an open issue, spans unrelated pull
requests, calls a proposal existing behavior, cites nonexistent code, has
unobservable criteria, says only "add tests", permits plaintext secrets, blocks
the UI without cancellation/resource bounds, has circular dependencies, or uses
a generic/duplicate branch. Reject model recommendations that are not available
in Codex or do not state an explicit reasoning effort and risk-based reason.

For desktop work, require reuse of the Python core and keep EPUB, HTML, cache,
glossary, review, resume, library, and HTTP logic out of Qt presentation code.
