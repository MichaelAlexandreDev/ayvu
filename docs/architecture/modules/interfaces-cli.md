# Shared application use-case matrix

Implementation: Implementation matrix consolidated in #158; this does not change ADR status.

This is the #123 shared matrix, updated after #158 dependencies.
It records delivered services and CLI presentation decisions. Desktop,
API and MCP must consume these contracts through their own composition roots;
the matrix neither introduces an interface nor accepts an additional ADR.

The product boundary remains defined by [Product scope](../../product-scope.md).
Current command examples remain documented in the [README](../../../README.md), and
translation compatibility is constrained by the
[translation workflow migration baseline](../../translation-workflow-migration-baseline.md).

## Contract vocabulary

- **Stable input** is a GUI-neutral value or policy that an application service
  may receive, such as a language pair, chapter selection, cache policy, output
  destination, retry policy, or overwrite policy.
- **Presentation decision** controls interaction or rendering, such as choosing
  common/developer wording, printing help, shell completion, or asking for a
  destructive-operation confirmation.
- **Current owner** names existing production modules, not the desired interface.
  EPUB, HTML, cache, glossary, HTTP, configuration, and filesystem logic must
  remain outside any CLI, Desktop, API, or MCP presentation adapter.
- **Future consumer** means an intended consumer after extraction. Desktop, API,
  and MCP are not implemented or promised by this document.
- **Cancellation** distinguishes current process-level `KeyboardInterrupt`
  handling from cooperative use cases. Inspect and visible-content export now
  accept reusable cancellation tokens; other legacy paths may still not.

## Use cases

Stable IDs are permanent references. Renaming a command must not silently rename
the capability it exposes.

| ID | Capability and invocations | Current owner and evidence | Inputs | Side effects and network | Progress, cancellation, errors, artifacts | Consumers |
| --- | --- | --- | --- | --- | --- | --- |
| `AYVU-UC-001` | Inspect EPUB metadata and inventory: `inspect` | [`formats/use_cases.py`](../../../src/ayvu/formats/use_cases.py), [`composition/builders/content.py`](../../../src/ayvu/composition/builders/content.py), [`formats/service.py`](../../../src/ayvu/formats/service.py), [`formats/epub/adapter.py`](../../../src/ayvu/formats/epub/adapter.py); [`test_content_inspection.py`](../../../tests/formats/use_cases/test_content_inspection.py), [`test_content_redaction.py`](../../../tests/formats/use_cases/test_content_redaction.py), [`test_cli_content.py`](../../../tests/interfaces/cli/test_cli_content.py) | EPUB source reference; optional cancellation and progress ports | Reads local EPUB only; no network, cache, project, job, artifact or destination writes | Typed inventory, format capabilities, source revision, bounded warnings, progress, cancellation and normalized failures; CLI renders the existing six-field table | CLI and framework-free consumer now; Desktop/API/MCP after adapter work |
| `AYVU-UC-002` | Explicit test: `test-translator` | `providers.discovery.ProviderDiscoveryService.test_provider`, via `composition.cli.cli_provider_session`; `tests/providers/test_provider_discovery_operations.py`, `tests/providers/libretranslate/test_provider_discovery_libretranslate.py`, `tests/interfaces/cli/test_cli_translator.py` | Explicit configuration, language pair and execution policy | Sends only the fixed public sample; no document | Typed result, bounded diagnostics and legacy output | CLI and framework-free consumer; future interfaces authorize their policy |
| `AYVU-UC-003` | Languages: `languages` and guided selection | `providers.discovery.ProviderDiscoveryService.languages`, via `composition.cli.cli_provider_session`; `tests/providers/test_provider_discovery_operations.py`, `tests/providers/libretranslate/test_provider_discovery_libretranslate.py`, `tests/translation/test_preflight_provider_routes.py`, `tests/interfaces/cli/test_cli_translator.py` | Explicit configuration and execution policy | Explicit language query; performs no test translation | Typed collection, normalized failures and CLI rendering | CLI and framework-free consumer |
| `AYVU-UC-004` | Translate one EPUB: `translate <epub>`, guided Translate | [`interfaces/cli/app.py`](../../../src/ayvu/interfaces/cli/app.py), [`translation/project.py`](../../../src/ayvu/translation/project.py), [`review/application.py`](../../../src/ayvu/review/application.py), [`composition/builders/review.py`](../../../src/ayvu/composition/builders/review.py), `review/local_packages.py`, `review/local_operations.py`, [`v1/epub.py`](../../../src/ayvu/v1/epub.py), [`v1/html_translate.py`](../../../src/ayvu/v1/html_translate.py), [`v1/cache.py`](../../../src/ayvu/v1/cache.py), [`translation/preflight.py`](../../../src/ayvu/translation/preflight.py), [`providers/libretranslate/client.py`](../../../src/ayvu/providers/libretranslate/client.py), [`recovery/checkpoint.py`](../../../src/ayvu/recovery/checkpoint.py), [`knowledge/memory/fuzzy.py`](../../../src/ayvu/knowledge/memory/fuzzy.py), [`formats/epub/validation.py`](../../../src/ayvu/formats/epub/validation.py), [`preferences/config_facade.py`](../../../src/ayvu/preferences/config_facade.py), [`interfaces/cli/progress.py`](../../../src/ayvu/interfaces/cli/progress.py); [`test_workflow_translation_resume.py`](../../../tests/interfaces/cli/test_workflow_translation_resume.py), [`test_workflow_route_review.py`](../../../tests/interfaces/cli/test_workflow_route_review.py), [`test_workflow_translation_reporting.py`](../../../tests/interfaces/cli/test_workflow_translation_reporting.py), [`test_cli_project_translation.py`](../../../tests/interfaces/cli/test_cli_project_translation.py), [`test_cli_progress.py`](../../../tests/interfaces/cli/test_cli_progress.py) | Input EPUB, language/profile, chapter selection, translator/cache/glossary/TM policies, execution controls, output/review policies | Reads the original without changing it; reads/writes SQLite cache and checkpoint; normally calls configured HTTP translator; `--cache-only` makes no translator request; may write translated EPUB, v2 review package, missing-text TXT, and Markdown reports | `TranslateProject` owns the durable translation result; review export consumes that exact result through a separate no-provider use case and D106 publication; cancellation and outcome contracts are shared | CLI now; framework-free application consumers now; Desktop/API/MCP after adapter work |
| `AYVU-UC-005` | Batch: `translate <epub>...` | `translation.batch.TranslateBatch`, `composition.builders.translate_batch.LocalTranslateBatch`, via `composition.cli.translate_batch`; `tests/interfaces/cli/test_translate_batch_output_planning.py`, `tests/interfaces/cli/test_translate_batch_execution_integration.py`, `tests/interfaces/cli/test_batch_bounded_validation.py` | Resolved order, destinations, policies and confirmations; same public restrictions | Complete bounded plan before execution; runs the same service per project; per-item publications and reports | Per-item events and results; cancellation, continuation and fail-fast belong to the service; Rich only projects | CLI and framework-free consumer |
| `AYVU-UC-006` | Preview: root `--preview` and guided flow | `composition.cli._translate_without_project` selects transient rich preview with a model label, otherwise the v1 facade; `tests/interfaces/cli/test_workflow_batch_preview.py`, `tests/kernel/test_cli_output_plan.py`, `test_cli_composition.py` | EPUB, confirmed destination, language and preview selection | Reads original; uses only required cache/provider; publishes preview without creating project/checkpoint | Retains compatibility callbacks and one final validation; report and path deliberately visible | CLI; a future interface must supply its own presenter |
| `AYVU-UC-007` | Resume: `resume [epub]` and guided offer | `recovery.service.ListInterruptedJobs`, `InspectJobRecoverability`, `RequestJobResume`, via `composition.cli.open_job_recovery`; `tests/interfaces/cli/test_cli_recovery.py`, `tests/recovery/test_job_recovery_*_integration.py` | Opaque reference, explicit decisions and saved configuration | Read-only inspection; resume authorizes continuation of the same `TranslateProject`; legacy checkpoint remains compatible | Recoverability result, bounded reasons, cancellation and durable chain; menu and confirmation belong to CLI | CLI and framework-free consumer |
| `AYVU-UC-008` | Extract reader-visible content: `extract` | [`formats/use_cases.py`](../../../src/ayvu/formats/use_cases.py), [`composition/builders/content.py`](../../../src/ayvu/composition/builders/content.py), [`formats/epub/adapter.py`](../../../src/ayvu/formats/epub/adapter.py), [`formats/epub/html_structure.py`](../../../src/ayvu/formats/epub/html_structure.py), [`formats/markdown/writer.py`](../../../src/ayvu/formats/markdown/writer.py); [`test_content_export.py`](../../../tests/formats/use_cases/test_content_export.py), [`test_markdown_writer_guards.py`](../../../tests/formats/markdown/test_markdown_writer_guards.py), [`test_markdown_writer_publication.py`](../../../tests/formats/markdown/test_markdown_writer_publication.py), [`test_markdown_writer_filesystem.py`](../../../tests/formats/markdown/test_markdown_writer_filesystem.py), [`test_content_redaction.py`](../../../tests/formats/use_cases/test_content_redaction.py), [`test_cli_content.py`](../../../tests/interfaces/cli/test_cli_content.py) | EPUB source reference, authorized output directory and overwrite policy; optional expected revision, cancellation and progress ports | Reads EPUB and atomically publishes one Markdown file per document; no network or durable project/artifact record; unrelated output files are preserved | Full output plan is validated before publication; typed per-file completed/failed/not-started outcomes, progress, cancellation and normalized failures; partial publication is explicit and no directory transaction is claimed | CLI and framework-free consumer now; Desktop/API/MCP after adapter work |
| `AYVU-UC-009` | Export, inspect, validate, and apply review packages: `translate --review-output`, `apply-review` | [`review/application.py`](../../../src/ayvu/review/application.py), [`composition/builders/review.py`](../../../src/ayvu/composition/builders/review.py), `review/local_packages.py`, `review/local_operations.py`, [`review/csv_import.py`](../../../src/ayvu/review/csv_import.py), [`review/exchange.py`](../../../src/ayvu/review/exchange.py), `review/exchange_codec.py`, `review/exchange_mapping.py`, [`interfaces/cli/app.py`](../../../src/ayvu/interfaces/cli/app.py); [`test_review_application_export_validate.py`](../../../tests/review/test_review_application_export_validate.py), [`test_review_application_apply.py`](../../../tests/review/test_review_application_apply.py), [`test_review_exchange_codec.py`](../../../tests/review/test_review_exchange_codec.py), [`test_review_exchange_mapping.py`](../../../tests/review/test_review_exchange_mapping.py), [`test_review_exchange_legacy.py`](../../../tests/review/test_review_exchange_legacy.py), [`test_review_exchange_epub_integration.py`](../../../tests/review/test_review_exchange_epub_integration.py), [`test_cli_project_review_application.py`](../../../tests/interfaces/cli/test_cli_project_review_application.py), [`test_cli_project_review_status.py`](../../../tests/interfaces/cli/test_cli_project_review_status.py), [`test_cli_project_review_races.py`](../../../tests/interfaces/cli/test_cli_project_review_races.py), [`test_cli_project_review_reconciliation.py`](../../../tests/interfaces/cli/test_cli_project_review_reconciliation.py), [`test_cli_translate_review_export.py`](../../../tests/interfaces/cli/test_cli_translate_review_export.py) | Exact `TranslateProjectResult` for export; bounded v1 inspection or v2 bytes plus source/project/machine/format context; output and overwrite policy for apply | V1 inspection and v2 validation are read-only; export/apply use no provider/network; export publishes the sensitive v2 package through D106; apply reconstructs, validates and publishes a new EPUB through D106 | Application commands/results are framework-neutral; validation evidence is a digest, not authorization; apply fully revalidates; v1 apply stops with `legacy_reexport_required`; job, progress, cancellation, errors, events and provenance carry bounded codes and opaque identities | CLI and framework-free application consumer now; future Desktop/API/MCP after adapter work |
| `AYVU-UC-010` | Inspect cache | [`knowledge/cache/admin.py`](../../../src/ayvu/knowledge/cache/admin.py), `composition/builders/cache_admin.py`, `knowledge/cache/admin_v2_store.py`, [`interfaces/cli/app.py`](../../../src/ayvu/interfaces/cli/app.py) | Cache reference, authorized namespace or explicit all, filters | Reads SQLite without creating absent caches; no network | Bounded aggregate metadata, progress, cancellation, warnings and normalized failures; default legacy gate remains | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-011` | Preview and clean cache | Same as UC-010 | Cache reference, authorized namespace, filters or explicit all; interface confirmation | Transactional deletion after preview revalidation; no network | Per-namespace aggregate counts, progress, cancellation, warnings and stale-preview failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-012` | Export cache | Same as UC-010 | Cache and destination references, authorized namespace or explicit all, filters and overwrite policy | Reads SQLite and atomically publishes sensitive JSON; no network | Count and actual JSON format version, progress, cancellation, warnings and normalized failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-013` | Import cache | Same as UC-010 | Cache and source references, caller-authorized external scope map, replace policy and revision-bound preview | Validates JSON and mapping before transactional SQLite mutation; no network; preview is discarded on refusal/failure or retained as audit after successful import | Counts, conflicts, duplicates, preview revision, progress, cancellation, warnings and normalized failures; sensitive audit artifact | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-014` | Discover, inspect, and open library books: guided Library | [`interfaces/cli/app.py`](../../../src/ayvu/interfaces/cli/app.py), [`library/service.py`](../../../src/ayvu/library/service.py), [`library/adapters.py`](../../../src/ayvu/library/adapters.py) | Configured library root and folder names, opaque book/entry references, optional reader command | Discovery reads local directory metadata without opening EPUB contents; an explicit open can launch a local reader | Grouped entries, bounded warnings, cooperative cancellation and normalized open outcomes; a new scan invalidates prior references | Guided CLI and framework-free service consumers; other interfaces must authorize reader launch explicitly |
| `AYVU-UC-015` | Preferences: first use, Settings and configuration reading | `preferences.service.get_preferences` / `update_preferences`, via `composition.cli.configuration_store`; facade `config.ConfigStore`, values `preferences.model`; `tests/preferences/test_preferences.py`, `tests/preferences/test_preferences_store.py`, `tests/preferences/test_config.py`, `tests/interfaces/cli/test_cli_root_processing.py` | V1 values and expected revision; resolved prompts | Selected JSON repository; reading creates no configuration; writing validates revision | Snapshot, warnings, refusal and persistence result; presentation retains messages and menus | CLI and framework-free consumer |
| `AYVU-UC-016` | Create, edit, preview, and select managed glossaries | [`knowledge/glossary/catalog.py`](../../../src/ayvu/knowledge/glossary/catalog.py), [`knowledge/glossary/catalog_store.py`](../../../src/ayvu/knowledge/glossary/catalog_store.py), [`composition/builders/glossary.py`](../../../src/ayvu/composition/builders/glossary.py), [`interfaces/cli/app.py`](../../../src/ayvu/interfaces/cli/app.py) | Trusted managed root, opaque references, entries and revisions | Bounded local JSON catalog; no network | Normalized failures and cooperative cancellation; divergent updates are unavailable; presentation owns prompts and paths | Guided CLI and framework-free consumers with a supplied repository |

## Proposed target ownership

This section title remains stable for #123 tests. The former
proposals below now point to actually delivered contracts.

| Use-case IDs | Delivered application owner | Ports/adapters in composition |
| --- | --- | --- |
| `AYVU-UC-001`, `AYVU-UC-008` | `formats.InspectContent`, `ExportVisibleContent` | `composition.builders.content`: formats and Markdown publication |
| `AYVU-UC-002`, `AYVU-UC-003` | `providers.discovery.ProviderDiscoveryService` | `composition.builders.providers`: explicit discovery, test and preflight |
| `AYVU-UC-004` | `translation.project.TranslateProject` | `composition.builders.translate_project`: execution, format, cache, validation and publication |
| `AYVU-UC-005` | `translation.batch.TranslateBatch` | `composition.builders.translate_batch`: bounded plan and items from the same service |
| `AYVU-UC-006` | Preview compatibility facade delivered by #145 | `composition.cli` connects `translate_without_project`; creates no second service |
| `AYVU-UC-007` | `recovery.service` and the same `TranslateProject` | Read-only evidence and legacy checkpoint through composition |
| `AYVU-UC-009` | `review.application` | `composition.builders.review` wires `review.local_packages` for codec/context and `review.local_operations` for reconstruction and D106 publication |
| `AYVU-UC-010`–`AYVU-UC-013` | `knowledge.cache.admin` | `composition.builders.cache_admin`: per-namespace repositories and interchange |
| `AYVU-UC-014` | `library.service.LibraryService` | `library.adapters`: local reading and explicitly requested opening |
| `AYVU-UC-015` | `preferences.service` | `config.ConfigStore` over `JsonPreferencesRepository`; values in `preferences.model` |
| `AYVU-UC-016` | `knowledge.glossary.catalog` | `composition.builders.glossary`: managed JSON catalog |

## Interface-only behavior

| ID | Current surface | Classification |
| --- | --- | --- |
| `AYVU-UI-001` | `--install-completion`, `--show-completion` | Shell integration only; not an application use case. |
| `AYVU-UI-002` | `--mode common|developer` | Selects wording/detail and guided-vs-command presentation; it must not change domain results. |
| `AYVU-UI-003` | `cache` command group | Navigation container for cache use cases; it has no independent application operation. |
| `AYVU-UI-004` | Guided Help, Back, and Exit choices; prompts and Rich tables | Interaction/rendering only. Prompt answers must be converted into stable inputs before a service is invoked. |

## Option boundary

The option boundary and the CLI surface snapshot are in the [CLI option boundary and surface snapshot](../cross-cutting/cli-surface.md); the maintenance rule below covers both documents.

## Guided workflow coverage

The guided main menu is not discoverable through Typer's command tree, so its
coverage is explicit:

| Guided choice | Mapping |
| --- | --- |
| Translate | `AYVU-UC-004` |
| Preview | `AYVU-UC-006` |
| Library | `AYVU-UC-014` |
| Settings | `AYVU-UC-015` |
| Glossaries | `AYVU-UC-016` |
| Help, Back, Exit, prompts, and rendering | `AYVU-UI-004` |

## Extraction constraints and known gaps

1. Application services must expose GUI-neutral request/result/error structures.
   They must not print, prompt, raise Typer exits, render Rich objects, or launch
   interface-specific flows.
2. Interface adapters may select presentation and authorization policies, but
   EPUB/HTML transformation, cache SQL, glossary rules, configuration storage,
   translation HTTP, checkpoint semantics, and output safety stay in reusable
   modules.
3. #159 [HTTP policy](../cross-cutting/network-policy.md) is per run: `none` blocks
   before DNS/socket/payload; `loopback` allows only validated loopback HTTP.
   It covers all current uses of GET `/languages` and POST `/translate`.
   External readers and the local server's network have independent authority.
4. Jobs and use cases provide structured results and events. Translation
   also retains delivered chapter/text/validation callbacks for legacy
   Rich progress; these callbacks invent no job or artifact state.
5. Cooperative cancellation and terminal results belong to services; the CLI
   also converts process interruption to legacy exit semantics.
6. The legacy checkpoint remains a live facade, coordinated with durable
   recovery through `recovery.service`; removing it would break existing resume files.
7. Provider diagnostics use the same `ProviderDiscoveryService` as framework-free
   consumers. This creates neither a universal diagnostic service nor a
   new network policy.
8. Any shared network boundary must require an explicit egress policy before API
   or MCP exposure: allowed schemes and hosts, redirect/proxy behavior, payload
   and response limits, and a denied/offline mode with zero transport. Credentials
   remain opaque adapter references and must never enter requests/results, stored
   configuration, logs, diagnostics, fixtures, or history. Book text sent to a
   translator is sensitive user content and errors must be redacted accordingly.
9. Filesystem authorization cannot live only in presentation adapters. Shared
   boundaries must revalidate allowed roots or capabilities, symlink/hardlink and
   archive traversal, overwrite intent, resource limits, and atomic output or
   recovery behavior before reading, writing, deleting, importing, or launching
   a local process.
10. Cache exports, review files, missing-text reports, translation memory, and
    authorized corpora can contain book text and are sensitive artifacts. Their
    consent, purpose, retention, provenance, redaction, and export authorization
    must be explicit. Imported cache or TM data must never become an approved
    corpus implicitly, and those stores remain logically separate.

## Maintenance rule

Every command option is classified within its command; `option_exclusions`
records when a command exposes more than one use case but an option is rejected
or not implemented for one of them. Universal and root options carry their
mappings explicitly. Any public Typer command, subcommand, or long option change
must update the JSON snapshot and the corresponding stable mapping in the same
change. The contract
test in [`test_shared_use_case_matrix.py`](../../../tests/architecture/test_shared_use_case_matrix.py)
compares this snapshot with the effective Typer command tree and validates its
local references. Guided-flow changes require a manual update to the guided table
until those choices have a separately inspectable registry.

## Final CLI composition (#158)

The installed entry point remains `ayvu = ayvu.interfaces.cli.app:app`. `interfaces/cli/app.py` and `interfaces/cli/progress.py`
handle Typer, common/developer mode, questions, confirmations, Rich, selected
paths, textual reports and exit codes. They import contracts/results
and named functions from `composition/cli.py`; they do not import concrete adapters.
`interfaces/cli/contracts.py` contains structural ports and shared errors without Typer/Rich.
`preferences/model.py` separates preference values from the persistent facade.

`composition/cli.py` is the only construction boundary selected by CLI.
Its named functions compose only the requested flow using predecessor
composition modules. There is no service registry, singleton, global instance
cache or dynamic discovery. Dependencies are passed to services
through their ports and explicit configuration; results, warnings and job identity
are returned without fabricating success. Desktop/API/MCP use their own roots
and the same services; they must not import `interfaces/cli/app.py` or reuse Rich objects.

Importing CLI and requesting any help neither opens repositories nor creates a provider
session. Inspect opens only the source; cache inspection uses the existing
adapter's read-only queries. Missing configuration is not created by
reading. Cache-only constructs no provider/session, resolves no secrets and performs no
DNS/socket/HTTP: pure configuration validation retains previous refusals.
The selected cache and, in durable translation, the selected project remain
necessary; no unrelated storage is opened or migrated. A new checkpoint
stays empty until an online translation's preflight safe point.

Composition now receives #159 per-request selection and shares its
snapshot with transport authorization and #128 use cases. An absent selection
records default `loopback`, never remote authority. #128's removal of the hidden test
translation remains. #159 adds only the options and messages
documented in `network-policy.md`. Safe publication, source immutability and cache/glossary rules
remain in delivered services/adapters. CLI reports retain previous writing,
file mode and names, without introducing a lock, hard link or new limit.

The normalized contract is frozen in `tests/fixtures/cli-post-predecessor.json`,
captured at SHA `3edd2c2813c592418189670dbef1866c3026780e` before refactoring.
It includes tree, aliases, parameters, requiredness, defaults, types, environment and help.
The #132 characterization remains intact in `tests/interfaces/cli/test_workflow_translation_resume.py`, `tests/interfaces/cli/test_workflow_route_review.py`, `tests/interfaces/cli/test_workflow_translation_reporting.py`, `tests/interfaces/cli/test_workflow_batch_preview.py`;
old CLI tests retain their assertions and only point canaries to
the new owners. Changing behavior and this baseline simultaneously requires
a documented compatibility decision and separate review. The test retains the
#158 baseline and separately checks the five `--network-policy` additions
requested by #159; it does not rewrite the historical fixture. #162 explicitly adds
`--tm-conservative-auto-apply` and `--tm-legacy-cache-compatibility`, off by
default, and clarifies the three TM help texts. The test checks these additions
and exact texts before comparing everything else with the frozen fixture.
In rich/v2 runtime, `--translation-memory` selects `MemoryRetrieval` per project;
the new selectors only choose `RetrievalPolicy`. Legacy runtime retains
previous selectors. There is no Desktop retrieval consumer today.

## CLI helper and facade inventory

| Use case | Helpers |
| --- | --- |
| `AYVU-UC-002`, `AYVU-UC-003`, `AYVU-UC-004`, `AYVU-UC-005`, `AYVU-UC-006`, `AYVU-UC-007` | `_selected_network_policy`, `_print_network_selection` |

Inventory of all symbols defined by presentation, checked by the
same #123 matrix test. Each group below references the service
or presentation action rows above. Functions performing IO delegate to composition;
rendering and selecting paths remain presentation responsibilities.

| Mapping | Current symbols from `interfaces/cli/app.py` |
| --- | --- |
| `AYVU-UI-004` | `_RouteView`, `TranslationRunResult`, `BatchTranslationResult`, `_BatchItemExit`, `ExecutionControls` |
| `AYVU-UI-004` | `main`, `create_app` |
| `AYVU-UC-001` | `inspect` |
| `AYVU-UC-002` | `test_translator` |
| `AYVU-UC-003` | `languages` |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-012`, `AYVU-UC-013` | `_cache_namespace`, `_cache_scope_map`, `_cache_warnings`, `cache_inspect`, `cache_clean`, `_cache_failure_hint`, `cache_export`, `cache_import` |
| `AYVU-UC-004`, `AYVU-UC-005` | `translate` |
| `AYVU-UC-007` | `resume` |
| `AYVU-UI-004` | `_print_expected_error` |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-013` | `_normalize_cache_before`, `_validate_cache_clean_options`, `_print_cache_summary`, `_print_cache_import_report`, `_plural_cache_entries` |
| `AYVU-UC-001`, `AYVU-UC-008` | `_print_epub_read_error`, `_print_content_failure` |
| `AYVU-UC-004`, `AYVU-UC-005`, `AYVU-UC-006` | `_resolve_source_language`, `_print_translation_plan`, `_execution_control_rows`, `_format_workers`, `_format_rate_limit`, `_format_retry_policy`, `_format_execution_summary`, `_format_number`, `_resolve_execution_controls`, `_resolve_performance_profile`, `_print_translation_route`, `_resolve_requested_profile`, `_resolve_target_language`, `_resolve_glossary_path`, `_parse_chapter_selection`, `_resolve_chapter_selection_for_display`, `_print_selected_chapters`, `_validate_translate_command_options`, `_validate_cache_only_options`, `_build_translation_memory_options`, `_validate_worker_options`, `_print_rich_memory_notices`, `_resolve_translation_output_dir`, `_run_translation`, `_translation_progress_display`, `_print_project_failure`, `_print_translation_failure` |
| `AYVU-UC-005` | `_run_batch_translation`, `_print_batch_plan`, `_print_batch_summary` |
| `AYVU-UC-006` | `_run_preview`, `_prepare_preview` |
| `AYVU-UC-008` | `extract` |
| `AYVU-UC-009` | `apply_review`, `_print_review_apply_failure` |
| `AYVU-UC-004`, `AYVU-UC-006`, `AYVU-UC-009` | `_validate_with_progress`, `_print_validation_warnings`, `_print_report`, `_has_glossary_summary`, `_print_glossary_warnings`, `_resolve_review_output_path`, `_print_review_export_failure` |
| `AYVU-UC-007` | `_print_interrupted_translation`, `_partial_chapter_value`, `_interrupted_output_message`, `_print_processing_translation_states` |
| `AYVU-UI-004` | `_run_guided_main_flow` |
| `AYVU-UC-015` | `_load_or_init_config`, `_load_existing_config_or_default`, `_refuse_invalid_translator_models`, `_init_default_language_config`, `_choose_initial_folder_names`, `_print_feature_folder_paths`, `_save_config` |
| `AYVU-UI-004` | `_GuidedAction`, `_guided_main_actions`, `_print_guided_main_menu`, `_handle_guided_main_choice` |
| `AYVU-UC-004` | `_run_guided_translation` |
| `AYVU-UC-006` | `_run_guided_preview` |
| `AYVU-UC-004` | `_choose_guided_translation_profile`, `_print_translation_profiles`, `_choose_guided_profile_glossary` |
| `AYVU-UC-016` | `_run_guided_glossaries`, `_print_guided_glossary_menu`, `_create_guided_glossary`, `_edit_guided_glossary`, `_preview_guided_glossary`, `_choose_guided_translation_glossary`, `_prompt_new_glossary_path`, `_prompt_guided_glossary_terms`, `_prompt_guided_glossary_entry`, `_prompt_required_text`, `_upsert_glossary_entry`, `_save_guided_glossary`, `_glossary_failure`, `_load_guided_glossary`, `_prompt_saved_glossary_path`, `_print_saved_glossaries`, `_print_guided_glossary_preview`, `_list_glossary_files` |
| `AYVU-UC-014` | `_run_guided_library`, `_print_library_books`, `_prompt_library_book`, `_print_library_warnings`, `_run_guided_library_book`, `_library_book_actions`, `_print_library_book_actions`, `_print_library_book_info`, `_open_library_book_path`, `_display_book_translations` |
| `AYVU-UC-015` | `_run_guided_settings`, `_print_settings`, `_print_settings_menu`, `_update_guided_settings`, `_settings_with_default_language`, `_settings_with_books_dir`, `_settings_with_folder_names`, `_settings_with_reader_app`, `_settings_with_model_label`, `_format_profile_names` |
| `AYVU-UI-004` | `_prompt_path`, `_prompt_folder_name`, `_prompt_optional_text` |
| `AYVU-UC-003` | `_choose_guided_target_language`, `_prompt_target_language_code`, `_print_guided_language_choices`, `_language_code_from_guided_choice`, `_languages_were_listed`, `_load_languages_for_guided_selection`, `_print_languages`, `_display_language_targets` |
| `AYVU-UC-007` | `_offer_detected_translation_resume`, `_ResumeRunner`, `_resume_translation`, `_print_resume_refusal`, `_select_resume_state`, `_print_resume_checkpoint`, `_has_checkpoint_progress`, `_checkpoint_chapter_value`, `_print_running_resume_states`, `_print_invalid_resume_states` |
| `AYVU-UC-004`, `AYVU-UC-005`, `AYVU-UC-006` | `_offer_markdown_report`, `_save_markdown_report`, `_save_missing_texts`, `_render_missing_texts`, `_missing_filename_stem`, `_print_cache_coverage_blocked`, `_render_markdown_report`, `_default_reports_dir`, `_next_available_report_path`, `_report_filename_stem`, `_safe_filename_part`, `_display_optional_path`, `_report_output_value` |
| `AYVU-UC-006` | `_ensure_preview_input_exists`, `_print_preview_output_location` |
| `AYVU-UC-004`, `AYVU-UC-006` | `_confirm_default_output_location`, `_original_epub_location_message`, `_prompt_output_path` |
| `AYVU-UI-004` | `_single_line`, `_format_terms`, `_format_counted_terms` |
| `AYVU-UC-004`, `AYVU-UC-006` | `_resolve_existing_output_conflict`, `_prompt_existing_output_action`, `_prompt_available_output_path`, `_cancel_existing_output`, `_confirm_existing_preview_overwrite` |
| `AYVU-UI-004` | `_translated_books_dir`, `_preview_books_dir`, `_reports_dir`, `_processing_dir`, `_uses_internal_storage_defaults` |
| `AYVU-UC-004`, `AYVU-UC-006` | `_translate_without_project`, `_translate_in_project` |
| `AYVU-UC-004`, `AYVU-UC-005` | `cache_coverage_blocked`, `translation_progress` |
| `AYVU-UC-007` | `_checkpoint_action` |
| `AYVU-UC-004`, `AYVU-UC-005` | `_report_stem_for`, `_missing_stem_for` |

The only removed function, `_count_cache_entries`, had no calls, Typer
registrations or compatibility consumers; the import/call inventory and
suite cover its removal. Execution/checkpoint/publication helpers were
moved, not discarded. Facades `config`, `resume`, `preflight`,
`composition.builders.providers`, `translation.batch_output` and `composition.builders.translate_project`
continue reexporting the same values/errors for existing consumers.
Private delegates `_translate_in_project` and `_translate_without_project`
remain in CLI for their characterization consumers; they contain no business logic.

Labeled durable `translate --cache-only --workers 1` uses local preflight and exact v2 evidence,
with normal project/job creation. Missing originals flow through `_save_missing_texts` unchanged:
ordered occurrences, explicit overwrite, automatic suffixes and `AcceptedItem.write_auxiliary` in
batch. Full-cache gaps preserve prior EPUB output and exit 1 after TXT/report; complete or allowed
partial valid output exits 0. Corrupt/overflow/storage failure emits no EPUB or partial TXT list.

Labeled root and guided previews use late preparation through `composition.cli.prepare_preview`.
They retain `OutputPlan.for_preview`, configured `books_dir`/`folders.preview`, `preview_dir`,
the Preview fallback and `<stem>-preview.epub`. Overwrite refusal returns 1 before preparation;
acceptance publishes a structurally valid EPUB. Validation warnings retain the EPUB and
report with exit 1. Preview offers no Markdown and creates no project,
job or checkpoint. Provider errors return 1 with previous output intact; keyboard interruption
retains the CLI cancellation exit. Without a label, preview messages, requests and v1 cache
behavior are preserved, including preparation before progress starts. G2's cache/partial-output
differences were accepted on 2026-11-13.
