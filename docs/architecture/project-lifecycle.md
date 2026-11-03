# ADR: Durable project identity and standalone CLI resolution

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-003`

Related issue: [#134 — Versioned local project manifest](https://github.com/MichaelAlexandreDev/ayvu/issues/134)

## Context

The current CLI stores per-translation checkpoints in
[`resume.py`](../../src/ayvu/resume.py). These record execution options and
progress, but do not establish a durable project identity. Shared application
workflows need a stable local reference that survives process restarts and
interface changes.

The [accepted modular boundaries](modular-monolith-boundaries.md) assign project
contracts to the core and concrete storage to adapters. The
[product threat model](product-threat-model.md) identifies filesystem, content,
and credential boundaries; it remains `Proposed`.

## Decision

Adopt `Project` as a durable unit of work. The first implementation is bounded
to the existing local EPUB workflow and the contract of #134:

- Allocate and persist an opaque project UUID in a versioned manifest. A path,
  content digest, or command invocation is not the project ID.
- Reference the original EPUB in its existing location and record its content
  fingerprint. Open it read-only; creating project state does not copy, move,
  or modify the original.
- Store managed project state beneath the configured local processing root.
  Derive that workspace from trusted Ayvu configuration, never document content
  or provider output.
- Resolve or create a compatible stored project automatically for
  `ayvu translate`, including the corresponding guided translation flow, without
  adding a project-selection prompt or changing output names and locations.
- Reuse the same compatible project for repeated requests. Concurrent lookup
  and creation must not allocate duplicate compatible projects. Changed or
  missing sources, incompatible requests, and ambiguous matches have explicit
  outcomes rather than silently selecting a project.
- Keep manifest data bounded to metadata and opaque references. Full source or
  translated text, resolved credentials, provider payloads, and UI objects do
  not belong in it. Treat paths and fingerprints as sensitive metadata.
- Publish validated manifest updates atomically with expected-revision checks.
  Interrupted writes and concurrent updates must preserve the prior valid
  state. Persistence failure stops dependent work rather than inventing an
  ephemeral project identity.
- Removing managed state must never automatically remove an original, output
  EPUB, cache, glossary, or unrelated project.

## Compatibility amendment

For this first project implementation, automatic project resolution replaces
the earlier proposed opt-in rollout for persistent projects. Local project-state
creation and reuse are accepted new side effects and must be documented before
the implementation is delivered. Existing translation prompts, provider intent,
output naming, and original immutability remain compatibility requirements.

This amendment concerns projects only. It does not change the separate proposed
rollout of additional translation providers.

## Consequences and limits

Translation will require local storage for bounded project metadata. Disk or
permission failures, stale source references, concurrent writers, and schema
incompatibility become explicit failure cases. Source fingerprinting adds local
I/O. Managed state must have a documented location and recovery behavior.

The exact manifest schema, filenames, locking mechanism, size limits, and Python
module layout are implementation details to review in #134. This decision does
not select SQLite for project storage or approve a general storage layout.
The later, separately accepted [DEC-012](local-project-storage.md) adds the
database/backup locations for #135 without expanding this DEC-003 acceptance.

Durable jobs, event storage, artifact payloads, provenance records, migrations,
legacy-checkpoint conversion, cleanup, Desktop, network-policy enforcement, and
credential-store adapters remain separate work. No other `DEC-*` status changes
through this acceptance.

## Required implementation evidence

Issue #134 must demonstrate canonical manifest round-trips, stable allocated
identity, compatible reuse, atomic revision updates, and deterministic
concurrency behavior through production contracts and the real storage adapter.
Synthetic fixtures must cover source changes, path aliases and escapes,
unsupported schemas, ambiguous matches, permission/disk failures, interrupted
writes, and redaction. CLI regression tests must preserve prompts and output
behavior and show no provider call during project resolution.

This ADR records a decision, not delivered functionality. Code changes, branch
creation, commits, publication, and issue completion retain their normal review
and approval requirements.
