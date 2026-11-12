# Where new code goes

Read this before adding code, whether you are a person or an AI agent. It summarizes the accepted
[package-structure decision](decisions/package-structure.md) (D1–D15); that decision remains authoritative.
Use the [PR checklist](#pr-checklist) when preparing a change.

## Choose the owner first

Put a rule with the capability that owns it, not with its caller or its storage technology (D1).
Start with `model.py`, `service.py` and `store.py`. Grow subpackages only past about six files or
2,000 lines. Introduce a port only when multiple implementations exist or really will.

In the table, **lower layers** means the rows below the owner's row in the layer map below, subject
to D2, D5 and D7 and the existing boundary contracts. Layer position alone does not authorize an import
of infrastructure or a framework into domain/application code. The old boundary check still applies.
File names with `<name>` are placement patterns, not existing files.

| Addition | Module and file | May import | Must not import / contain | Test location (D12) |
| --- | --- | --- | --- | --- |
| Domain rule | The owning capability's `model.py` or focused rule file; for example [content/model.py](../../src/ayvu/content/model.py), [jobs/model.py](../../src/ayvu/jobs/model.py) or [knowledge/glossary/rules.py](../../src/ayvu/knowledge/glossary/rules.py). Coordinate use cases in its `service.py`. | Own module and lower-layer public domain APIs. | Higher or independent same-row modules; concrete I/O, UI, composition or v1. | `tests/<package>/test_<rule>.py`, mirroring subpackages. |
| CLI command | [interfaces/cli/commands/](../../src/ayvu/interfaces/cli/commands/__init__.py)`<name>.py`; registration in [app.py](../../src/ayvu/interfaces/cli/app.py). | CLI presentation/contracts, public domain/use-case values and named factories from [composition/cli.py](../../src/ayvu/composition/cli.py). | Stores, vendor clients, composition builders, other interfaces or v1; business rules and adapter construction. | `tests/interfaces/cli/test_<command>.py`. |
| Desktop screen | [interfaces/desktop/presentation/views/](../../src/ayvu/interfaces/desktop/presentation/views/__init__.py)`<name>.py`; presentation state in [viewmodels/](../../src/ayvu/interfaces/desktop/viewmodels/__init__.py). | Desktop presentation, public domain/use-case values; wiring through [composition/desktop.py](../../src/ayvu/composition/desktop.py). | CLI/server, CLI root, concrete stores or providers, builders or v1; business rules. | `tests/interfaces/desktop/test_<screen>.py`. |
| Translation provider | [providers/](../../src/ayvu/providers/__init__.py)`<name>/adapter.py` and its client, following [libretranslate/adapter.py](../../src/ayvu/providers/libretranslate/adapter.py) and [client.py](../../src/ayvu/providers/libretranslate/client.py). | Own [provider contracts](../../src/ayvu/providers/ports.py) and lower public APIs, including the network adapter where appropriate. | Translation orchestration, knowledge, formats, composition or v1. Wire it in composition, never in a domain API. | `tests/providers/<name>/test_*.py`. |
| Format | [formats/](../../src/ayvu/formats/__init__.py)`<name>/adapter.py`, following [epub/adapter.py](../../src/ayvu/formats/epub/adapter.py). | Own [format contracts](../../src/ayvu/formats/contracts.py), content and lower public APIs. | Providers, knowledge, pipeline orchestration, review, composition or v1. | `tests/formats/<name>/test_*.py`. |
| Store or I/O adapter | The owning module's `store.py` or focused adapter, such as [jobs/store.py](../../src/ayvu/jobs/store.py), [artifacts/destination.py](../../src/ayvu/artifacts/destination.py), [projects/schema.py](../../src/ayvu/projects/schema.py) or [network/http.py](../../src/ayvu/network/http.py). | Own model/ports, lower public APIs and the I/O libraries allowed for that adapter. | Higher/same-row independent modules; composition or new v1 dependencies. Do not load it from the package's model/use-case API. | `tests/<package>/test_<adapter>.py`, including I/O integration tests. |
| AI role | No executable module/file is assigned yet. The [assistance-authority proposal](decisions/ai-assistance-authority.md) remains `Proposed`; no runtime or Python role contract is authorized. | No new executable imports now. The proposal places a future accepted implementation outside domain/application packages, wired explicitly by composition. | Do not invent an AI package, role implementation, SDK or generic assistant under this guide. D13 also defers new features until refactoring ends. | No executable role tests yet; documentary checks in `tests/architecture/`. Future runtime tests must mirror the accepted owner. |
| Test | `tests/<package>/test_<behavior>.py`; CLI under `tests/interfaces/cli/`. Output-plan and translation-option values belong in `tests/kernel/`. | Production APIs for the behavior under test, test support and fixtures. Architecture tests may inspect source/contracts. | Production must never import tests; do not put tests under `src/` or add core membership merely by directory. | Mirror the production package and reason to change. |
| Shared fake or fixture | [tests/support/](../../tests/support/__init__.py)`<capability>_fakes.py` for cross-package reuse; `tests/<package>/*_support.py` for package-local support, as in [EPUB support](../../tests/formats/epub/epub_format_support.py). | The relevant production contracts and other focused test support. | Production code must not depend on it; avoid copying the same fake into several test files. | Consumers stay in their mirrored test packages; support files are not test suites. |

## Layer and module map

This is D3's order, from top to bottom. Lower layers never import higher ones. Independent modules on
the same row do not import each other. Imports must also obey the public API and technical boundary
rules below. Each link opens the module's current API or the decision that explains its absence.

| Layer | Modules and responsibilities |
| --- | --- |
| 1 | [interfaces](../../src/ayvu/interfaces/__init__.py): CLI and desktop presentation; server is planned, not created. |
| 2 | [composition](../../src/ayvu/composition/__init__.py): roots and [builders](../../src/ayvu/composition/builders/__init__.py) select and construct adapters (D5). Editions belong here (D6, pending REF-17); modules never check editions. |
| 3 | [v1](../../src/ayvu/v1/__init__.py): quarantined default runtime without a model label, pending retirement in REF-18 (D7). Not a home for new capabilities. |
| 4 | [recovery](../../src/ayvu/recovery/__init__.py): resume/recovery orchestration and its store. Pure translation reuse rules stay in [translation/resume_reuse.py](../../src/ayvu/translation/resume_reuse.py). |
| 5 | [review](../../src/ayvu/review/__init__.py): applying and exchanging reviews. It may read translation results; translation never reads review. |
| 6 | [translation](../../src/ayvu/translation/__init__.py): plans, pipeline, document/project/batch orchestration and [context](../../src/ayvu/translation/context/__init__.py). Memory retrieval is a [pipeline stage](../../src/ayvu/translation/stages/memory_retrieval.py), not knowledge. |
| 7 | [projects](../../src/ayvu/projects/__init__.py): project model/lifecycle, storage/schema and [profiles](../../src/ayvu/projects/profiles/__init__.py). |
| 8 | [preferences](../../src/ayvu/preferences/__init__.py): global preference values, validation and storage. |
| 9 | [knowledge](../../src/ayvu/knowledge/__init__.py): independent [cache](../../src/ayvu/knowledge/cache/__init__.py), [memory](../../src/ayvu/knowledge/memory/__init__.py) and [glossary](../../src/ayvu/knowledge/glossary/__init__.py) APIs; [providers](../../src/ayvu/providers/__init__.py): translator contracts, discovery, capabilities and adapters; [formats](../../src/ayvu/formats/__init__.py): format contracts, extraction, reconstruction and validation. These three modules are independent. |
| 10 | [artifacts](../../src/ayvu/artifacts/__init__.py): artifact records, publication and destinations. Composition injects format publication through a port; artifacts does not import formats. |
| 11 | [provenance](../../src/ayvu/provenance/__init__.py): lineage and durable records tied to jobs. |
| 12 | [jobs](../../src/ayvu/jobs/__init__.py): job lifecycle, events, execution and storage. |
| 13 | [content](../../src/ayvu/content/__init__.py): documents, units and protection; [network](../../src/ayvu/network/__init__.py): network policy and HTTP adapter; [library](../../src/ayvu/library/__init__.py): library discovery and reader adapters; `application`: only truly cross-cutting contracts, [not created after REF-04](decisions/package-structure.md#open-questions). These are independent. |
| 14 | [kernel](../../src/ayvu/kernel/__init__.py): identifiers, UTC, cancellation, failures/warnings and common language values. Shared by at least three modules; imports no Ayvu module (D4). Migration infrastructure has its own [kernel.sqlite API](../../src/ayvu/kernel/sqlite/__init__.py). |

The remaining flat [content_use_cases.py](../../src/ayvu/content_use_cases.py) has an unresolved home:
moving it into content would create the formats cycle recorded in the decision's file map. Its
existing import ignore is not a placement precedent. The root [ayvu/__init__.py](../../src/ayvu/__init__.py)
is package metadata, not a shared-capability bucket.

## Imports and wiring

- **Public APIs (D2):** import names exported by the other package's `__init__.py`, including subpackage
  APIs such as `ayvu.knowledge.cache` and `ayvu.kernel.sqlite`. Do not reach into private implementation
  files. Imports within a module are free under D2, but other boundary rules still apply.
- **Adapters (D2):** I/O adapters are public modules themselves, not re-exports from the model/use-case
  API. Examples are `ayvu.jobs.store` and `ayvu.network.http`. The explicit adapter list used by
  [test_public_api.py](../../tests/architecture/test_public_api.py) lives in
  [public_api_support.py](../../tests/support/public_api_support.py). A new adapter must be reflected
  in the existing boundary/API declarations; an arbitrary plain submodule is not public.
- **Roots (D5):** each composition root is a public module; `composition/__init__.py` exports nothing.
  An interface imports only its own root. Only `composition/cli.py` imports builders from outside
  composition. Roots select/build adapters, hold no business rule and run no translation stage.
  The review builder siblings retain only their declared internal edges pending the owner's decision.
- **Quarantine (D7):** only composition imports v1. Its modules are individually public while its
  `__init__.py` exports nothing. Existing lower-layer v1 imports have issue-owned, shrinking ignores;
  do not copy them. v1's broad dependency allowance does not override the other boundary contracts.
- **Moves (D8):** update all imports in the same change; do not leave compatibility shims.

## Size is an alarm, responsibility is the criterion

[D10](decisions/package-structure.md#d10-single-responsibility-with-a-size-ratchet) limits new files to
400 lines, tests included, and new functions to 80 lines. Existing larger files/functions are recorded
in [tests/size_ratchet.json](../../tests/size_ratchet.json) and may only shrink. After shrinking an entry,
run `scripts/size_ratchet.py --update` to lower the ledger.

Ask: **does this file have one reason to change?** Split along different responsibilities, never only
to pass a line limit. Search for the existing owner and reusable rule before creating another one.

## What tools prove, and what review must prove

The existing checks implement D2, D10, D11 and D14:

| Check | What it detects |
| --- | --- |
| [import-linter contracts](../../pyproject.toml), run by [test_import_contracts.py](../../tests/architecture/test_import_contracts.py) | Layer direction, same-row independence and quarantine. Every ignored import names its removal issue; a stale ignore fails. |
| [test_size_ratchet.py](../../tests/architecture/test_size_ratchet.py) and [size_ratchet.py](../../scripts/size_ratchet.py) | New size violations and growth beyond the existing ledger. |
| [test_public_api.py](../../tests/architecture/test_public_api.py) | Cross-package private imports and package APIs that load registered adapters, SQLite or HTTP libraries. |
| The older [architecture boundary checks](../../tests/architecture/architecture_boundaries_support.py), exercised by [tree](../../tests/architecture/test_architecture_tree_validation.py), [import](../../tests/architecture/test_architecture_import_resolution.py) and [CLI construction](../../tests/architecture/test_architecture_cli_construction.py) tests | Declared owners/edges, technical dependencies, cycles and CLI construction/reachability; absorbed over time (D11). |
| [CI core selection](../../tests/conftest.py) and [test_ci_stages.py](../../tests/ci/test_ci_stages.py) | Approved invariant families, including architecture and documentation links. PRs run core on Python 3.11; pushes to main run the full suite; tags/manual runs use the full matrix. Moving a test into a folder does not make it core. |

These checks do not establish that a rule belongs in the chosen module, that an implementation avoids
duplication, or that a split has a real responsibility boundary. They do not classify a PR as mechanical
or behavioural, nor prove that persisted identities were untouched. The
[identity tests](../../tests/architecture/test_persisted_identities.py) freeze known tags, versions and
digests, but reviewing changes to fingerprint inputs and persisted semantics remains necessary (D9).

Use the PR checklist to record that review. Under D13, keep one migration open at a time; a PR is either
mechanical or behavioural, never both, and moves use `git mv`. Preserve persisted identities; changing
one requires a migration, version bump and explicit decision. Prove preservation with both suite modes,
Docker, identity checks, architecture metrics and, when `src/` changes, comparisons with main
(71 CLI scenarios, 42 translate/resume scenarios and e2e with a real HTTP server). See the
[release workflow](../release-workflow.md) for test stages and the release metrics checkpoint.

Update docs and CHANGELOG with the change. Under D15, new committed text is English; existing Portuguese
docs await REF-20. The owner keeps CHANGELOG entries in Portuguese. Changing an architecture rule needs
the decision and its contract updated in the same reviewed PR (D14); this guide changes no rule.

## PR checklist

Copy this into the pull request description, ticking each item or saying why it does not apply.

- [ ] The rule or code belongs to the right module and respects its public API and layer.
- [ ] Existing rules and implementations are reused; no duplication was introduced.
- [ ] Every split follows a reason to change, never only a size limit.
- [ ] The PR is mechanical or behavioural, never both (D13).
- [ ] Persisted identities are untouched, or a migration, version bump and explicit decision are included (D9).
- [ ] Documentation and CHANGELOG are updated as applicable.
- [ ] Verified: the full suite plainly and with `GITHUB_ACTIONS=true` (`scripts/check.sh`); the Docker stage
  (`scripts/docker-tests.sh`); when `src/` changes, the comparisons with main; the persisted-identity tests,
  `scripts/arch_metrics.py` and `git diff --check`.
