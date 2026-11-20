# Ayvu review lenses

Use the live issue and current repository as the contract. These lenses organize
the review; they do not replace targeted reading of the architecture.

## 1. Scope and Git

- One issue, one branch, one coherent review surface.
- Branch, base, dependencies, files, and acceptance criteria match the live
  issue.
- User-owned changes are neither reverted nor silently included.
- No dependency-owned work, unrelated cleanup, hidden publication, or premature
  issue closure.

## 2. Architecture and compatibility

- Affected decisions (D1–D15), module ownership, layer, public API, no
  recreated v1 runtime (D7), and the new-module checklist in AGENTS.md
  remain satisfied.
- Domain and application dependencies point inward.
- Interfaces adapt shared use cases instead of duplicating business rules.
- Formats do not choose providers, cache, network policy, or presentation.
- Providers do not know formats, widgets, project paths, or SQLite schemas.
- Persisted and public contracts are versioned with compatible migration,
  rollback, or recovery.
- Proposed decisions remain Proposed until human acceptance.

## 3. Correctness and tests

- Tests exercise production code or a real boundary and fail for the intended
  regression.
- Original files, output paths, markup, ordering, spacing, IDs, assets, and
  partial-failure behavior remain correct where applicable.
- Cache identity, idempotency, cancellation, concurrency, atomic writes, and
  recovery cover edge and failure cases.
- Fakes represent external boundaries rather than testing themselves.
- Skipped Qt tests do not count as Qt evidence.

## 4. Security and privacy

- User content is untrusted data and cannot grant tool, file, network, or policy
  authority.
- Strict offline and denied policies produce no hidden transport or fallback.
- Credentials remain opaque and absent from DTOs, storage, logs, diagnostics,
  tests, and history.
- Paths, symlinks, hardlinks, archives, redirects, payload size, resources, and
  destructive operations are bounded.
- Cache, approved translation memory, and authorized corpus remain separate.
- Consent, purpose, retention, provenance, and redaction are explicit wherever
  sensitive data crosses a boundary.
