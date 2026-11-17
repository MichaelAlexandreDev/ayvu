# TDD for Ayvu issue implementation

Read this reference during readiness to choose only the issue-relevant seams,
cases, and validation for the approval plan. Apply its test and implementation
steps only after that plan receives explicit approval. It adapts the red-green
discipline credited in
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) to Ayvu's
evidence and safety requirements.

## Test at observable seams

Test behavior through the highest stable production interface that can prove
the issue criterion. Prefer a shared application use case, public module
interface, CLI boundary, persistence contract, or format round-trip over a
private helper. Tests should survive an internal refactor that preserves the
same behavior.

Do not mock internal Ayvu modules merely to assert calls, call order, or private
implementation. Fake external boundaries such as HTTP providers, native stores,
process launchers, clocks, and platform integration. A fake must model the real
boundary closely enough to exercise production orchestration rather than test
itself.

Expected values need an independent source of truth: a literal from the live
issue, a worked fixture, an accepted contract, or characterized current
behavior. Do not recompute the expected value with the same algorithm being
tested.

## Red-green slices

For each approved seam:

1. Add one focused test for an observable behavior or regression.
2. Run it and confirm that it fails for the intended reason.
3. Implement only enough production behavior to make it pass.
4. Re-run the focused test and relevant neighboring tests.
5. Continue with the next independent behavior.

Use characterization tests when preserving an existing workflow and contract
tests when introducing a port, adapter, serialization, migration, or versioned
boundary. For migrations, prove old-version input, idempotency, interruption,
backup or rollback, recovery, unsupported-version failure, and unchanged data
on failed publication as applicable.

## Ayvu fixtures and invariants

Cover only the issue-relevant invariants selected in the approved plan. The
list below is a risk checklist, not authority to expand the issue scope.

- Use synthetic minimal EPUBs and other generated fixtures; never depend on a
  private book, PDF, cache, glossary, credential, or user path.
- Use `tmp_path` for caches, projects, extracted artifacts, reports, databases,
  and output files.
- Use no real LibreTranslate, provider, secret store, or external network in the
  standard suite.
- Prove source immutability and a distinct output for material transformations.
- For EPUB and HTML, cover ZIP structure, uncompressed `mimetype`, visible-text
  selection, ignored nodes, ordering, whitespace, tags, attributes, links,
  namespaces, assets, placeholders, and atomic failure where relevant.
- For adversarial archive cases, bound entry count, expanded and per-entry size,
  and compression ratio; reject absolute, traversal, drive-prefixed, NUL, and
  link entries; and prove extraction remains inside a fresh temporary root.
- For cache and persisted contracts, cover complete identity, version handling,
  compatibility, partial failure, and recovery. Keep operational cache,
  approved translation memory, and authorized corpus semantically separate.
- Treat document text, metadata, filenames, translated responses, and imported
  review data as untrusted input with no tool, path, markup, network, or policy
  authority.

## Completion evidence

Run focused tests during each cycle. Before handoff, run `uv run pytest`,
`git diff --check`, relevant CLI smoke checks, and any migration, packaging,
architecture, or platform validation required by the live issue. Report skips
and coverage limits explicitly. A green suite does not replace direct evidence
for every acceptance criterion, and skipped Qt tests do not prove Qt behavior.
