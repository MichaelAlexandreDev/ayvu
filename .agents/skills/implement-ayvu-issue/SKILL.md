---
name: implement-ayvu-issue
description: Implement one published, open Ayvu issue through a gated and evidence-based workflow. Use when the user asks to start, execute, or finish a specific existing issue. Do not use for drafting issues, broad roadmap work, or multiple issues at once.
---

# Implement Ayvu Issue

Complete one issue with one branch and one reviewable change set. Preserve the
existing CLI behavior and architectural invariants unless the live issue
explicitly changes them.

## Phase 1: readiness, read-only

Before editing:

1. Read AGENTS.md, then the [architecture guide](../../../docs/architecture/README.md) and [where new code goes](../../../docs/architecture/cross-cutting/where-code-goes.md) for the layers you will touch.
2. Fetch the live issue body and metadata read-only. Confirm number, title,
   state, labels, branch, priority, estimate, acceptance criteria, tests, and
   every hard dependency.
3. Capture branch, base, HEAD, main, origin/main, remotes, and all preexisting
   changes. Do not clean, normalize, chmod, restore, or absorb user changes.
4. Prove that every dependency artifact exists in main. Issue state, a local
   file, or an unmerged branch is insufficient.
5. Read the directly involved production modules and tests. Read README.md or
   docs when behavior visible to users may change. Check pyproject.toml and
   uv.lock when dependencies or entry points may change.
6. When behavior changes or needs regression protection, read
   `references/tdd.md` to identify the smallest relevant test seams and
   red-green strategy for the approval plan. Do not edit tests yet.
7. Read only the decision records and module contracts relevant to the issue.
   Cite the affected decisions (D1–D15 of the package-structure decision), the
   owning modules and their contracts.
8. Compare the live issue, architecture, and repository. Stop for a human
   decision when they diverge materially.

Read-only investigation may be delegated to ayvu_issue_auditor and, for
cross-boundary work, ayvu_architect. Delegated agents must return evidence and
must not edit files. The primary agent remains the only writer.

## Approval gate

Present a plan containing:

- objective and observable outcome;
- exact scope;
- files allowed and prohibited;
- compatibility, data, security, and migration risks;
- test seams and the red-green strategy for new or changed behavior;
- targeted and full validation;
- branch and base;
- preexisting changes that must be preserved.

Wait for explicit approval before the first edit. Creating or switching a branch
also requires specific authorization. Approval to edit does not authorize a
commit, push, issue mutation, pull request, or merge.

## Phase 2: implementation

After approval:

1. Recheck branch and worktree state.
2. Before creating, moving or splitting a module, follow `$ayvu-place-code`.
   For a translation provider, follow `$ayvu-add-provider`.
3. Make the smallest change that satisfies the issue. Do not implement contracts
   owned by dependencies or perform unrelated cleanup.
4. Keep presentation thin. Domain and application code must not depend on Typer,
   Rich, Qt, requests, MCP, SQLite, ebooklib, or concrete parser types.
5. Preserve original immutability, output safety, markup fidelity, cache
   identity, explicit network policy, opaque credentials, provenance, and
   partial-failure semantics relevant to the change.
6. When behavior changes or needs regression protection, follow
   `references/tdd.md` in red-green slices at the seams approved in the plan.
   Tests must exercise production code or a real boundary and fail under the
   regression they claim to prevent.
7. Use fakes for external services and `tmp_path` for local state.
8. Update user or architecture documentation only when the issue requires it.
   Keep ADRs and architecture decisions Proposed until human acceptance.

Never let multiple agents edit the shared worktree concurrently.

## Phase 3: validation and handoff

Run targeted tests during development, `scripts/check-fast.sh` after each edit
session, then the validation ladder in `$ayvu-validate` for the kind of change
(full suite in both modes, comparisons with `main` when `src/` changes behaviour,
the Docker stage for behavioural changes).

Report the final scope, changed files, test results, skips, acceptance-criteria
coverage, residual risks, and unrelated preexisting changes. Stop before commit,
push, issue editing, pull request, or merge and request each authorization
separately.
