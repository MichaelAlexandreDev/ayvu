# Package structure and dependency rules

Status: Accepted
Decision-Date: `2026-11-10`
Owners: the Ayvu repository owner and maintainer (acceptance); Claude Code (refactoring execution by the owner's decision of 2026-11-10).
Issue: [#267 — REF-02](https://github.com/MichaelAlexandreDev/ayvu/issues/267).

Base: `main` at `7902aba`. The starting measurements are in [refactoring-baseline.md](../cross-cutting/refactoring-baseline.md),
reproducible with `scripts/arch_metrics.py`.

## Acceptance record

Accepted on 2026-11-10 by the repository owner, through explicit approval in the development session
(translated quotation: "I accept REF-02"), with no restrictions on D1 to D14, the target tree, the file map or the sequence.

Two additions accepted at the same time:
- **D15**, the language rule below.
- **REF-20**, which reorganizes `docs/architecture/` after the code moves, and is added to the sequence.

This record was first proposed in Portuguese (PR #269). Under D15, it is kept in English from this change on.
The translation changes no decision.

## Context

The [modular monolith boundaries](modular-monolith-boundaries.md) are already `Accepted`. They name the
modules (Projects, Jobs, Content, Translation, Knowledge, Formats, Providers, Review, Provenance, Artifacts,
Policies) and the layers (Domain, Application, adapters, composition roots). The folders do not reflect
them:
- `src/ayvu` has 116 files and 37,935 lines, almost all on one level;
- 30 files are over 400 lines and 8 over 800; 18 functions are over 80 lines;
- 12 pairs of module groups import each other in both directions.

After studying the alternatives, the owner chose this composition:
- a modular monolith outside, with hexagonal ports and adapters inside each module, in proportion;
- a pipeline at the centre of translation;
- internal plugins for translators, formats and AI roles;
- client-server only for the Lite server and mobile;
- editions chosen in composition;
- strategic DDD;
- a strangler-fig migration.

The owner's answers on 2026-11-10:
- the shared core is named `kernel/`;
- files are limited to 400 lines, tests included, and functions to 80;
- `import-linter` checks the boundaries;
- no compatibility shims;
- the refactoring comes first, with no new feature issues until it ends;
- the v1 runtime is to be retired, not kept;
- the layer order (D3) is accepted.

## Decisions

### D1. Modular monolith, hexagonal in proportion
- Folders by capability. Inside each module, start flat: `model.py`, `service.py`, `store.py`.
- A module grows subpackages (`domain/`, `application/`, `adapters/`) only past about 6 files or 2,000 lines.
- A port (interface) only where more than one implementation exists or really will.

### D2. One public API per module
- Each module's `__init__.py` lists what is public.
- Other modules import only that API: `ayvu.knowledge.cache`, never `ayvu.knowledge.cache.admin_v2_store`.
- Imports inside a module are free.
- A subpackage's `__init__.py` is also a public API (for example `ayvu.kernel.sqlite`). A plain submodule
  of another package never is.
- Adapters are public modules of their own (REF-08, #276): the modules that do I/O, such as
  `jobs/store.py`, `artifacts/destination.py`, `projects/schema.py` or `network/http.py`. The package's
  `__init__.py` exports only the model and the use cases. So code that needs a job's state or an artifact
  record does not load SQLite, HTTP or the filesystem code, the same reason `ayvu.kernel.sqlite` has its own
  API. `tests/architecture/test_public_api.py` lists the adapter modules and checks that no package import loads them.
- A package whose parts are independent exports nothing itself and leaves the API to its subpackages, as
  `knowledge` does with `knowledge.cache`, `knowledge.memory` and `knowledge.glossary` (REF-09).

### D3. One-way layers
A lower layer never imports a higher one. Modules on the same row do not import each other unless the row
says otherwise.

| Layer (top to bottom) | Modules |
| --- | --- |
| 1 | `interfaces` (cli, desktop, server) |
| 2 | `composition` |
| 3 | `v1` |
| 4 | `recovery` |
| 5 | `review` |
| 6 | `translation` |
| 7 | `projects` |
| 8 | `preferences` |
| 9 | `knowledge`, `providers`, `formats` |
| 10 | `artifacts` |
| 11 | `provenance` |
| 12 | `jobs` |
| 13 | `content`, `network`, `library`, `application` |
| 14 | `kernel` |

REF-12 (#280) gave `review` a row above `translation`: applying a review reads the translation result,
and translation never reads review, so the row decides it without an exception.
REF-07 (#275) and REF-08 (#276) moved `preferences` and `provenance` to follow their real imports.
`provenance` reads the job each record belongs to, inside the same transaction, so it sits above `jobs`.
`preferences` validates a declared model label with `provenance`, so it sits above `provenance`. Projects,
knowledge, providers and translation import both, and neither imports them, so each has its own row and the
imports are layer rules instead of exceptions.

`application/` holds only the truly cross-cutting contracts left over from `cli_contracts.py`.

### D4. A deliberately small `kernel/`
- It takes identifiers, UTC validation, cancellation, typed failures and warnings, the SQLite migration
  runner and the common domain values (languages).
- Only what three or more modules use goes in. `kernel` imports no other Ayvu module.
- Implemented in REF-04 (#272). The moved classes keep their names (`JobError`, `JobValidationError`,
  `JobEventError`, `JobFailure`, `JobWarning`, `JobCancellationError` and others), so callers catch the same
  exception types as before. The SQLite migration runner is infrastructure, so it has its own API in
  `ayvu.kernel.sqlite` instead of being re-exported by `ayvu.kernel`.

### D5. Composition leaves the modules
- The `*_composition.py` files go to `composition/builders/`. The roots go to `composition/cli.py`,
  `composition/desktop.py` and, later, `composition/server.py`.
- A root selects and builds adapters. It runs no translation stage and holds no business rule.
- Implemented in REF-05 (#273). Each root is a public module of its own, the one exception to D2: an
  interface imports only its root, so the desktop does not load the CLI wiring, and later an edition does
  not load another's. `composition/__init__.py` exports nothing. Only `composition/cli.py` imports
  builders from outside composition. Review builder siblings use only their declared internal edges;
  their location remains unchanged pending an owner decision. The layers contract keeps every other
  module from importing `composition`.

### D6. Editions in composition
- `composition/editions.py` declares what each edition enables (Lite, Free, AI).
- No module checks the edition. Paid plugins can live in separate closed packages.
- Defining the editions depends on the owner and on #100 (REF-17).

### D7. The v1 runtime in quarantine
- v1 is today's default when no model label is set. Its files move to `v1/`.
- Implemented in REF-06 (#274). `v1` is a required layer below `composition`, and a contract keeps the
  interfaces from importing it. The modules below it that still import v1 do so under ignores owned by the
  issue that removes each one; their cycles with `v1` are listed below. Each v1 module is public by itself,
  so `v1/__init__.py` exports nothing: no package API is designed for code that is going away.
- `v1` may import any module; no module imports `v1`, only `composition`.
- The owner decided to retire v1. How and when is REF-18, after listing what only v1 does.

### D8. No shims
- Moving a file updates every import in the same PR. No file is left only re-exporting the new place.
- The entry points (`ayvu`, `ayvu-desktop`) change path in `pyproject.toml`; the commands stay the same.
- Rationale: the repository is private, not on PyPI, and nobody outside imports Ayvu.

### D9. Frozen persisted identities
- `tests/architecture/test_persisted_identities.py` (REF-01) freezes the domain tags, versions and digests.
- No class that enters a fingerprint is renamed (`CreateCandidate`, `DeriveRevision`, `ReviewRevision`).
- Changing an identity needs a migration, a version bump and an explicit decision in the PR.

### D10. Single responsibility with a size ratchet
- New files: at most 400 lines, tests included. New functions: at most 80 lines.
- Today's larger files and functions are listed with their current size and may only shrink.
- Enforced by `tests/architecture/test_size_ratchet.py` with the ledger `tests/size_ratchet.json`; `scripts/size_ratchet.py --update` lowers entries after a file or function shrinks (REF-03).
- Size is the alarm. The criterion is "does this file have one reason to change?".

### D11. Boundaries checked by `import-linter`
- A development dependency only. A layers contract mirrors D3, and independence contracts separate the
  modules of one layer.
- Today's violations are listed as exceptions that may only shrink.
- The contracts live in `[tool.importlinter]` in `pyproject.toml` and run in `tests/architecture/test_import_contracts.py`. Every ignored import names its REF issue, and an ignore that stops matching fails the check (REF-03).
- D2 is enforced by `tests/architecture/test_public_api.py`.
- The old boundary test (`tests/architecture/test_architecture_*.py`) is absorbed over time.

### D12. Tests mirror the packages
- `tests/<module>/test_*.py`; shared fakes and fixtures in `tests/support/`.
- CLI tests live in `tests/interfaces/cli/`; output-plan and translation-option values are tested in `tests/kernel/`. `CORE_TEST_FILES` follows the approved families.

### D13. Process
- One migration open at a time.
- Each PR is either mechanical (moving files and rewriting imports) or behavioural, never both.
- `git mv` keeps history.
- Every PR proves nothing changed:
  - the suite in both modes and the Docker stage;
  - the comparisons with `main` (71 CLI scenarios, 42 translate and resume scenarios, e2e with a real HTTP server);
  - the identity test;
  - `scripts/arch_metrics.py`, with the numbers going down.
- No new feature issues until the refactoring ends.

### D14. Keeping the architecture over time
- **CI checks:** the ratchet (D10), the import contracts (D11) and a public-API check (D2).
- **A ["where new code goes" guide](../cross-cutting/where-code-goes.md)** in `docs/architecture/` and AGENTS.md, read by people and AI agents.
- **A PR checklist** for what tools miss: a rule in the wrong module, duplication, splitting only to pass a limit.
- **Changing a rule is allowed but visible:** this decision and the contract change in the same reviewed PR.
- **`scripts/arch_metrics.py` in the release checklist**, to follow the trend.

### D15. Language of committed text
- Everything committed to the repository is written in English: documentation, decision records, code
  comments, commit messages and PR descriptions.
- Text that is not committed (conversation, local drafts) is in Portuguese.
- Existing Portuguese documents are translated during REF-20.
- Text shown to users by the CLI is product UI, not documentation, and is outside this rule unless the
  owner decides otherwise. Whether README and CHANGELOG follow it is to be confirmed by the owner.

## Target tree

```text
src/ayvu/
  kernel/          ids, clock, cancellation, failures, values; sqlite/ (migrations, contracts)
  network/         policy, http
  preferences/     model, service, store, config_facade
  library/         service, adapters
  provenance/      model, service, store
  content/         model, use_cases, protection
  application/     contracts (cross-cutting only)
  jobs/            model, events, execution, service, store
  artifacts/       model, service, store, destination
  formats/         contracts, service, local; epub/ (archive, adapter, io, html_structure, validation); markdown/
  knowledge/       cache/ (identity, schema, repository, interchange, admin, admin_store, admin_v2_store, admin_v2_queries, admin_v2_files, admin_v2_export, admin_v2_records, admin_v2_import);
                   memory/ (fuzzy; approved/ model, service, store); glossary/ (rules, catalog, catalog_store)
  providers/       ports, capabilities, discovery, service, network, session;
                   libretranslate/ (client, adapter)
  projects/        model, service, store, schema; profiles/ (model, service, store, glossary_store)
  translation/     ports, plan, pipeline, engine, document, chunking, preflight, project, batch, batch_output;
                   stages/ (memory_retrieval); context/ (model, execution, source)
  review/          application, exchange, exchange_model, exchange_parts, exchange_mapping,
                   exchange_codec, exchange_files, csv_export, csv_import
  recovery/        service, store
  v1/              translation, epub, html_translate, cache, cache_check, cache_format, cache_stream,
                   cache_admin_store, resume, resume_scan
  composition/     cli, desktop, editions (later); builders/
  interfaces/      cli/ (app, commands/, render, progress); desktop/; server/ (later)
```

## File map

The table of where each flat module was planned to go is in the [refactoring file map](../cross-cutting/refactoring-map.md).

## Cycles and what removes them

| Pair | Cause | Removed by |
| --- | --- | --- |
| jobs ↔ projects | `job_recovery_store` uses projects; `project_schema` uses jobs | REF-08 (`recovery/`) |
| jobs ↔ provenance | `job_recovery_store` uses provenance; provenance uses job identities | REF-04 (kernel) and REF-08 |
| artifacts ↔ jobs | `job_recovery_store` uses artifacts; artifacts use job identities and cancellation | REF-04 and REF-08 |
| jobs ↔ translation | `job_recovery` calls `translate_project` | REF-08 |
| interfaces ↔ jobs | `resume` uses `cli_contracts` | REF-04 (splitting `cli_contracts`) |
| interfaces ↔ providers | `provider_composition` uses `cli_contracts` | REF-04 and REF-05 |
| interfaces ↔ translation | `preflight`, `batch_output_policy` and `translate_project_composition` use `cli_contracts` | REF-04 and REF-05 |
| content ↔ formats | Resolved: inspection and export orchestration now lives in `formats/use_cases.py`; content does not import formats | REF-07, owner's decision of 2026-11-12 |
| formats ↔ review | `epub_io` reads and writes the review CSV | REF-10 |
| formats ↔ translation | `html_structure` and `html_translate` use `translation_protection` and the pipeline | REF-06 (`html_translate` to v1) and REF-07 (`content/protection`) |
| knowledge ↔ translation | `memory_retrieval` (in knowledge) uses `translation_plan` and `translation_protection` | REF-07 and REF-09 (`memory_retrieval` to the pipeline) |
| artifacts ↔ formats | `artifact_destination` calls `format_local` and `format_service` | REF-08 (a port) |
| formats ↔ v1 | `epub_io` uses the v1 cache, translator and HTML translation; `validation` uses `extract_visible_text` | REF-10 |
| knowledge ↔ v1 | cache administration reads the v1 cache schema and format; `translation_memory` reads the v1 cache | REF-09 and REF-18 |
| translation ↔ v1 | `preflight` builds the v1 translator and opens the v1 cache | REF-11 |

## Sequence

| Issue | What it does | Size |
| --- | --- | --- |
| REF-03 | Size ratchet, `import-linter` with today's exceptions, public-API check | M |
| REF-04 | Extract `kernel` and split `cli_contracts` | M |
| REF-05 | Move composition out of the modules | M |
| REF-06 | Quarantine v1 | M |
| REF-07 | Move the leaf modules: network, preferences, library, provenance, content | M |
| REF-08 | Move jobs, artifacts and projects; create `recovery/`; a port from artifacts to formats | L |
| REF-09 | Move knowledge; `memory_retrieval` to the pipeline | M |
| REF-10 | Move formats; take review out of `epub_io` | M |
| REF-11 | Move providers and translation | M |
| REF-12 | Move review | S |
| REF-13 | Interfaces: desktop and the CLI skeleton; entry points | S |
| REF-14 | Split `cli.py` by command group (about 10 PRs) | L |
| REF-15 | Split the modules over 800 lines (one issue per family) | L |
| REF-16 | Tests mirroring the packages; split CLI base tests | M |
| REF-17 | Composition roots per edition | M |
| REF-18 | Inventory and retirement of v1 | M |
| REF-19 | "Where new code goes" guide, AGENTS.md section and PR checklist | S |
| REF-20 | Reorganize `docs/architecture/` by module, one decision header, index, English (D15) | M |

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Technical layers only (global `domain/`, `services/`, `adapters/`) | Spreads each capability over three folders and removes no cycle between capabilities |
| Full hexagonal in every module | Bureaucracy for simple modules such as preferences and library |
| Vertical slices per feature | Ayvu shares a lot of rules (cache, glossary, pipeline); slices would duplicate them |
| Microservices | Network, deployment and consistency costs with no benefit for a local app with one maintainer |
| Compatibility shims | Nobody outside imports Ayvu; they would double every path |
| Rewriting from scratch | Loses the fixes accumulated so far and delivers nothing for months |

## Open questions

1. The features of each edition (REF-17) depend on the owner.
2. How to retire v1, and what happens to old caches and resume files, depends on the REF-18 inventory.
3. ~~Whether `application/` still needs to exist after REF-04.~~ Answered by REF-04: nothing in `cli_contracts.py` was
   cross-cutting, so `application/` is not created. Its optional layer stays in the contract until REF-20 tidies it.
4. Whether README and CHANGELOG follow D15.

## Acceptance gate

Met on 2026-11-10 (see the acceptance record). A later change to D1 to D15, the tree or the sequence needs
a reviewed change to this record, together with any contract it affects.

- [x] D1 to D14 accepted, with no restrictions
- [x] target tree and file map accepted
- [x] sequence REF-03 to REF-19 accepted, with REF-20 added
- [x] D15 added
