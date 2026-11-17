# AGENTS.md

Rules for AI agents and contributors working on Ayvu. Claude Code reads this file through `CLAUDE.md`;
Codex and other agents read it directly. Personal, machine-specific notes belong in an untracked
`CLAUDE.local.md` or `AGENTS.local.md`, never here.

## What Ayvu is

A local-first Python CLI that translates EPUB books through a translation server on the user's machine
(LibreTranslate-compatible), keeping everything but the visible text exactly as it was.

Invariants that no change may break:

- The original EPUB is never modified; output is a new `.epub` (translation or preview).
- Only text a reader sees is translated. HTML, CSS, images, links, the table of contents, internal file
  names and the ZIP layout are preserved, and `mimetype` stays uncompressed.
- Every translated segment is cached, so work resumes and is never requested twice.
- Ayvu never removes DRM, downloads books or helps distribute protected content.
- Book text is private: it leaves the machine only through an explicitly selected and consented provider.

## Read before changing code

1. [Architecture guide](docs/architecture/README.md): the index of modules, decisions and cross-cutting rules.
2. [Where new code goes](docs/architecture/cross-cutting/where-code-goes.md): the layer map and the PR checklist.
3. [Package structure decision](docs/architecture/decisions/package-structure.md): decisions D1 to D15 below.
4. The module you will touch, its contract in `docs/architecture/modules/`, and its tests.

Use the knowledge graph before broad searches (see [graphify](#graphify)). Do not read the whole repository.

## Architecture rules

These are enforced by tests and `import-linter`; a change that needs to break one needs an accepted
decision record first, not a workaround.

- **Layers (D3).** From top to bottom: `interfaces` → `composition` → `v1` → `recovery` → `review` →
  `translation` → `projects` → `preferences` → `knowledge` | `providers` | `formats` → `artifacts` →
  `provenance` → `jobs` → `content` | `network` | `library` → `kernel`. A lower layer never imports a higher
  one; modules on the same row do not import each other.
- **One public API per module (D2).** Import a package through its `__init__` exports. Adapter modules
  that are public on their own are listed in `tests/support/public_api_support.py` (`ADAPTER_MODULES`).
- **Composition builds adapters (D5).** Only `composition` selects and constructs concrete adapters. The
  CLI imports named factories from `ayvu.composition.cli`, never a builder or an adapter directly.
- **v1 is quarantined (D7).** `src/ayvu/v1/` is the legacy runtime, being retired (REF-18). Never add a
  capability there, and never import it from a new module.
- **No shims (D8).** When code moves, update every importer in the same PR; leave no re-export behind.
- **Frozen persisted identities (D9).** Cache keys, fingerprints, schema versions, checkpoint formats and
  job identities change only with a migration, a new version and an explicit decision.
- **Size (D10).** Files up to 400 lines and functions up to 80, tests included. Larger legacy entries are
  listed in `tests/size_ratchet.json` and may only shrink: `scripts/size_ratchet.py --update` lowers
  entries and never raises one. Split by responsibility, never by squeezing code to pass the limit.
- **Boundaries are checked (D11).** `uv run lint-imports` must keep every contract.
- **Tests mirror the packages (D12).** `src/ayvu/<pkg>/x.py` is tested in `tests/<pkg>/test_x*.py`; shared
  support lives in `tests/support/` or `tests/<pkg>/*_support.py`.
- **One kind of change per PR (D13).** A PR is mechanical (moves, renames, no behaviour change) or
  behavioural, never both. When a fix in a ratchet-listed file needs room, a separate mechanical commit may
  extract code inside the same PR, and the PR body says why.
- **Language (D15).** Everything committed is English: code, docs, commit messages, PR bodies. The
  `CHANGELOG.md` stays Portuguese. Messages the CLI shows to users are product UI and stay as they are.

### New module checklist

A new production module needs, in the same PR:

- an owner in `tests/support/architecture_owners.py`;
- every import edge it adds declared in the edge inventories under `tests/support/architecture_*edges.py`;
- an entry in `ADAPTER_MODULES` if other packages import it directly;
- a line in its module contract under `docs/architecture/modules/`, and in the package-structure tree when
  it changes the layout.

The architecture tests fail and name what is missing.

## Domain rules

- **Runtimes.** With a model label (`translator_models.<translator>` in the configuration) the rich
  runtime runs: durable jobs, provenance and the v2 cache. Without one, v1 runs, until step 4 of REF-18
  makes the label required. New behaviour goes into the rich runtime only.
- **Providers.** A translator is a provider adapter behind `ayvu.providers` contracts: a rich request and
  result, honest capability declarations, and a `ProducerIdentity`. Every result passes the strict result
  check (placeholders, protected parts, markup); never trust a provider to keep structure.
- **Cache identity.** Anything that can change an output (provider, model, prompt, pipeline revision,
  glossary, profile) is part of the semantic fingerprint or the cache scope. The glossary is applied after
  translation, including to cached text.
- **HTML.** Translate visible text only. Skip `script`, `style`, `code`, `pre`, `kbd`, `samp`, `svg`,
  `math`, comments, doctypes, XML declarations and processing instructions. Never use `get_text()` as the
  translation flow. Keep leading and trailing whitespace of text nodes and the order of chunks. Changes to
  HTML translation need tag-preservation tests.
- **Network.** All HTTP goes through `ayvu.network`. The default policy allows loopback HTTP only;
  `--network-policy none` blocks everything. Never add a direct socket, a new HTTP client or a fallback to a
  remote host.
- **Secrets and privacy.** Credentials are opaque references; never store a secret in configuration,
  checkpoints, logs, reports, provenance, fixtures or commits. Book text, caches, exports and review
  packages are private data.

## Working agreement

- **Plan first.** For any change to code, tests, scripts, workflows or behaviour: read first, then present
  the goal, scope, files, risks and validation, and wait for the maintainer's approval before editing,
  unless the maintainer authorised the task in writing.
- **One issue, one branch, one reviewable change set.** A dependency counts as done only when its artifact
  is in `main`. Preserve pre-existing changes you did not make.
- **Git.** Commit, push, open PRs and merge only when the maintainer asks. Never use `gh pr merge --admin`,
  never force-push `main`, never rewrite published history without an explicit request.
- **Decisions.** Decision records stay `Proposed` until the maintainer accepts them in writing. An issue or
  a draft does not accept a decision.

## Validation

Run what the change needs, in this order:

| Check | Command | When |
| --- | --- | --- |
| Fast gate | `scripts/check-fast.sh` | After every edit session; about 20 seconds |
| Full suite, both modes | `scripts/check.sh` | Before every push; the opt-in hook is `scripts/hooks/pre-push` |
| Docker stage | `scripts/docker-tests.sh <target>` | Before merging a behavioural PR; run targets one at a time, since parallel runs add load-only flakes |
| Real CLI comparisons | compare against `main` with real CLI runs | When `src/` changes user-visible behaviour |
| Docs only | `git diff --check` and the documentation tests | When only Markdown changes |

Tests use fakes, never a real translator or network. CLI tests isolate `HOME`, `XDG_CONFIG_HOME` and the
working directory with `tmp_path`, because CLI defaults point at real user folders. A new test must fail
without the change it protects. A green suite alone does not prove a change is complete.

## Documentation

Update `README.md`, `docs/guide/` or `docs/architecture/` when a command, flag, default, cache, glossary,
resume, validation, report or output format changes. Add a `CHANGELOG.md` entry (in Portuguese) for user-
visible changes. Architecture documents stay under 400 lines, and the documentation tests check the
links of the README, the guide and the architecture documents.

## Private and local files

Never edit, commit, copy or expose without an explicit request:

- `.ayvu-pessoal/`, `CLAUDE.local.md`, `AGENTS.local.md` and other personal notes;
- `.epub` and `.pdf` files, SQLite caches, `.cache/`, `glossary.json`;
- exported caches, review packages and anything else holding book text.

## graphify

This project has a knowledge graph in `graphify-out/` with god nodes, community structure and cross-file
relationships. When the user types `/graphify`, use the installed graphify skill first.

- When `graphify-out/graph.json` exists, run `graphify query "<question>"` before your first grep, search or
  source read for a codebase question, and put this rule in every subagent prompt that explores code. Read
  raw files only for lines the graph or an earlier read located. Use `graphify path "<A>" "<B>"` for
  relationships and `graphify explain "<concept>"` for focused concepts.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation. Read `graphify-out/GRAPH_REPORT.md`
  only for broad architecture review.
- After modifying code, run `graphify update .` (AST only, no API cost).
- A committed `graphify-out/graph.json` is rewritten whole on every build, so a large diff to it is
  expected; do not flag it as churn.
