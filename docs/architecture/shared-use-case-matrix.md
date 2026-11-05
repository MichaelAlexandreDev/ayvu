# Shared application use-case matrix

Status: `Proposed`

This document is the auditable contract for extracting application services that
can be shared by the CLI and future Desktop, API, and MCP interfaces. It records
the current behavior; it does not accept an architecture decision, introduce a
new interface, or move responsibilities out of the current modules.

The product boundary remains defined by [Product scope](../product-scope.md).
Current command examples remain documented in the [README](../../README.md), and
translation compatibility is constrained by the
[translation workflow migration baseline](../translation-workflow-migration-baseline.md).

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
| `AYVU-UC-001` | Inspect EPUB metadata and inventory: `inspect` | [`content_use_cases.py`](../../src/ayvu/content_use_cases.py), [`content_composition.py`](../../src/ayvu/content_composition.py), [`format_service.py`](../../src/ayvu/format_service.py), [`epub_format.py`](../../src/ayvu/epub_format.py); [`test_content_use_cases.py`](../../tests/test_content_use_cases.py), [`test_cli.py`](../../tests/test_cli.py) | EPUB source reference; optional cancellation and progress ports | Reads local EPUB only; no network, cache, project, job, artifact or destination writes | Typed inventory, format capabilities, source revision, bounded warnings, progress, cancellation and normalized failures; CLI renders the existing six-field table | CLI and framework-free consumer now; Desktop/API/MCP after adapter work |
| `AYVU-UC-002` | Probe translator health: `test-translator` | [`cli.py`](../../src/ayvu/cli.py), [`translator.py`](../../src/ayvu/translator.py); [`test_translator.py`](../../tests/test_translator.py) | Endpoint, language pair, timeout, retries, rate/backoff policy | Sends a fixed sample to the configured HTTP translator; no local artifact | No progress or cooperative cancellation; translator failures are expected outcomes; translated probe text is the result | CLI now; Desktop diagnostics/API/MCP after extraction |
| `AYVU-UC-003` | Discover translator languages: `languages` | [`cli.py`](../../src/ayvu/cli.py), [`translator.py`](../../src/ayvu/translator.py); [`test_translator.py`](../../tests/test_translator.py) | Endpoint, timeout, retries, rate/backoff policy | Reads the configured HTTP translator; no persistent write | No progress or cooperative cancellation; transport/protocol failure is an expected outcome; language collection is the result | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-004` | Translate one EPUB: `translate <epub>`, guided Translate | [`cli.py`](../../src/ayvu/cli.py), [`translate_project.py`](../../src/ayvu/translate_project.py), [`review_application.py`](../../src/ayvu/review_application.py), [`review_composition.py`](../../src/ayvu/review_composition.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py), [`cache.py`](../../src/ayvu/cache.py), [`preflight.py`](../../src/ayvu/preflight.py), [`translator.py`](../../src/ayvu/translator.py), [`resume.py`](../../src/ayvu/resume.py), [`translation_memory.py`](../../src/ayvu/translation_memory.py), [`validation.py`](../../src/ayvu/validation.py), [`config.py`](../../src/ayvu/config.py), [`cli_progress.py`](../../src/ayvu/cli_progress.py); [`test_workflow_characterization.py`](../../tests/test_workflow_characterization.py), [`test_cli_translate_project.py`](../../tests/test_cli_translate_project.py), [`test_cli_progress.py`](../../tests/test_cli_progress.py) | Input EPUB, language/profile, chapter selection, translator/cache/glossary/TM policies, execution controls, output/review policies | Reads the original without changing it; reads/writes SQLite cache and checkpoint; normally calls configured HTTP translator; `--cache-only` makes no translator request; may write translated EPUB, v2 review package, missing-text TXT, and Markdown reports | `TranslateProject` owns the durable translation result; review export consumes that exact result through a separate no-provider use case and D106 publication; cancellation and outcome contracts are shared | CLI now; framework-free application consumers now; Desktop/API/MCP after adapter work |
| `AYVU-UC-005` | Translate an EPUB batch: `translate <epub>...` | [`cli.py`](../../src/ayvu/cli.py); [`test_cli.py`](../../tests/test_cli.py), [`test_workflow_characterization.py`](../../tests/test_workflow_characterization.py) | Ordered input paths, output directory, language/profile, chapter selection, translator/cache/glossary/TM and execution policies, plus continue/fail-fast policy. Multiple inputs reject `--output` and `--review-output`; `--missing-output` is accepted by the current CLI but its explicit path is not forwarded to batch items | Runs the core translation effects per input. It may write one translated EPUB and one Markdown report per item; cache-only misses produce an automatically named missing-text TXT in the reports directory, not the path requested through `--missing-output`; no review CSV is produced by batch today | Per-book and inner translation progress are CLI-owned today; interruption is process-level; result is a batch summary plus per-book EPUB/report paths or errors | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-006` | Build a translation preview: root `--preview`, guided Preview | [`cli.py`](../../src/ayvu/cli.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py); [`test_cli.py`](../../tests/test_cli.py) | EPUB path, target language, current configured defaults | Reads the original; may use cache and configured HTTP translator; writes a new preview EPUB and never mutates the original | CLI progress/errors only; no cooperative cancellation contract; preview EPUB is the artifact | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-007` | Resume an interrupted translation: `resume [epub]` | [`cli.py`](../../src/ayvu/cli.py), [`resume.py`](../../src/ayvu/resume.py); [`test_resume.py`](../../tests/test_resume.py), [`test_cli.py`](../../tests/test_cli.py) | Optional EPUB selector and target language; saved checkpoint supplies remaining inputs | Reads/writes checkpoint and cache, may call the saved HTTP translator, and writes the planned output | Resumes chapter-level progress; `KeyboardInterrupt` may preserve state; corrupt/ambiguous/missing checkpoint and changed input are failures; checkpoint is file-backed state, not a durable job/service contract | CLI now; future consumers only after durable state and cancellation contracts are designed |
| `AYVU-UC-008` | Extract reader-visible content: `extract` | [`content_use_cases.py`](../../src/ayvu/content_use_cases.py), [`content_composition.py`](../../src/ayvu/content_composition.py), [`epub_format.py`](../../src/ayvu/epub_format.py), [`html_structure.py`](../../src/ayvu/html_structure.py), [`markdown_writer.py`](../../src/ayvu/markdown_writer.py); [`test_content_use_cases.py`](../../tests/test_content_use_cases.py), [`test_cli.py`](../../tests/test_cli.py) | EPUB source reference, authorized output directory and overwrite policy; optional expected revision, cancellation and progress ports | Reads EPUB and atomically publishes one Markdown file per document; no network or durable project/artifact record; unrelated output files are preserved | Full output plan is validated before publication; typed per-file completed/failed/not-started outcomes, progress, cancellation and normalized failures; partial publication is explicit and no directory transaction is claimed | CLI and framework-free consumer now; Desktop/API/MCP after adapter work |
| `AYVU-UC-009` | Export, inspect, validate, and apply review packages: `translate --review-output`, `apply-review` | [`review_application.py`](../../src/ayvu/review_application.py), [`review_composition.py`](../../src/ayvu/review_composition.py), [`review_import.py`](../../src/ayvu/review_import.py), [`review_exchange.py`](../../src/ayvu/review_exchange.py), [`cli.py`](../../src/ayvu/cli.py); [`test_review_application.py`](../../tests/test_review_application.py), [`test_review_exchange.py`](../../tests/test_review_exchange.py), [`test_cli_translate_project.py`](../../tests/test_cli_translate_project.py), [`test_cli.py`](../../tests/test_cli.py) | Exact `TranslateProjectResult` for export; bounded v1 inspection or v2 bytes plus source/project/machine/format context; output and overwrite policy for apply | V1 inspection and v2 validation are read-only; export/apply use no provider/network; export publishes the sensitive v2 package through D106; apply reconstructs, validates and publishes a new EPUB through D106 | Application commands/results are framework-neutral; validation evidence is a digest, not authorization; apply fully revalidates; v1 apply stops with `legacy_reexport_required`; job, progress, cancellation, errors, events and provenance carry bounded codes and opaque identities | CLI and framework-free application consumer now; future Desktop/API/MCP after adapter work |
| `AYVU-UC-010` | Inspect cache | [`cache_admin.py`](../../src/ayvu/cache_admin.py), [`cache_admin_store.py`](../../src/ayvu/cache_admin_store.py), [`cli.py`](../../src/ayvu/cli.py); [`test_cache_admin.py`](../../tests/test_cache_admin.py), [`test_cache_admin_integration.py`](../../tests/test_cache_admin_integration.py) | Cache reference and filters | Reads SQLite without creating absent caches; no network | Bounded metadata and normalized failures | CLI and framework-free consumers |
| `AYVU-UC-011` | Preview and clean cache | Same as UC-010 | Cache reference, filters or all; interface confirmation | Transactional deletion after revalidation; no network | Counts, progress, cancellation and normalized failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-012` | Export cache | Same as UC-010 | Cache and destination references, filters and overwrite policy | Reads SQLite and atomically publishes sensitive JSON; no network | Counts, progress, cancellation and normalized failures | CLI and framework-free consumers |
| `AYVU-UC-013` | Import cache | Same as UC-010 | Cache and source references, replace policy | Validates JSON before transactional SQLite mutation; no network | Counts, progress, cancellation and normalized failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-014` | Discover, inspect, and open library books: guided Library | [`cli.py`](../../src/ayvu/cli.py), [`library_service.py`](../../src/ayvu/library_service.py), [`library_adapters.py`](../../src/ayvu/library_adapters.py) | Configured library root and folder names, opaque book/entry references, optional reader command | Discovery reads local directory metadata without opening EPUB contents; an explicit open can launch a local reader | Grouped entries, bounded warnings, cooperative cancellation and normalized open outcomes; a new scan invalidates prior references | Guided CLI and framework-free service consumers; other interfaces must authorize reader launch explicitly |
| `AYVU-UC-015` | Read and update preferences: guided Settings; configuration consumed by other flows | [`cli.py`](../../src/ayvu/cli.py), [`config.py`](../../src/ayvu/config.py); [`test_config.py`](../../tests/test_config.py), [`test_cli.py`](../../tests/test_cli.py) | Default target, books directory, feature folder names, reader command, existing profiles | Reads/writes local configuration; no network | No progress/cancellation; validation/write failures are outcomes; configuration file is the artifact | Guided CLI now; Desktop after extraction; API/MCP require a separately authorized configuration boundary |
| `AYVU-UC-016` | Create, edit, preview, and select glossaries: guided Glossaries and translation input | [`cli.py`](../../src/ayvu/cli.py), [`glossary.py`](../../src/ayvu/glossary.py); [`test_glossary.py`](../../tests/test_glossary.py), [`test_cli.py`](../../tests/test_cli.py) | Glossary path, ordered terms, preserve/translate rule, preferred translation | Reads/writes local glossary JSON; no network | No progress/cancellation; invalid JSON/rules and write errors are outcomes; glossary JSON is the artifact | Guided CLI now; Desktop after extraction; API/MCP require explicit filesystem authorization |

## Proposed target ownership

These were logical module boundaries proposed for extraction, not an accepted
package layout. `AYVU-UC-001` and `AYVU-UC-008` are implemented by #147 in the
modules named below; the remaining rows stay proposed and do not imply that
their target modules exist today. Application modules orchestrate each use case;
ports describe required capabilities; concrete EPUB, SQLite, filesystem, process,
and HTTP implementations remain adapters.

| Use-case IDs | Proposed application owner | Required ports/adapters |
| --- | --- | --- |
| `AYVU-UC-001` (implemented) | `ayvu.content_use_cases.InspectContent` | Format registry and read-only inspection port |
| `AYVU-UC-002`, `AYVU-UC-003` | `ayvu.application.diagnostics` | Translator capability/health port governed by network policy |
| `AYVU-UC-004`, `AYVU-UC-005` | `ayvu.application.translation` | EPUB transformer, translator, cache, checkpoint, glossary, TM, validation, progress-event, and artifact ports |
| `AYVU-UC-006` | `ayvu.application.preview` | Translation core, cache, translator, validation, and preview artifact ports |
| `AYVU-UC-007` | `ayvu.application.resume` | Checkpoint, input-identity, cache, translator, progress-event, and output ports |
| `AYVU-UC-008` (implemented) | `ayvu.content_use_cases.ExportVisibleContent` | Visible-content format capability and non-durable Markdown writer port |
| `AYVU-UC-009` | `ayvu.review_application` | Implemented commands/results and use cases; `ayvu.review_composition` supplies the v2 codec/mapper, EPUB reconstruction, repositories and D106 publication ports; legacy v1 inspection remains read-only |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-012`, `AYVU-UC-013` (implemented) | `ayvu.cache_admin` | Cache query/mutation and import/export artifact ports |
| `AYVU-UC-014` (implemented) | `ayvu.library_service` | Discovery and authorized open coordination; `ayvu.library_adapters` supplies local filesystem and reader adapters |
| `AYVU-UC-015` | `ayvu.application.configuration` | Configuration store and filesystem-policy ports |
| `AYVU-UC-016` | `ayvu.application.glossary` | Glossary store/validation and filesystem-policy ports |

## Interface-only behavior

| ID | Current surface | Classification |
| --- | --- | --- |
| `AYVU-UI-001` | `--install-completion`, `--show-completion` | Shell integration only; not an application use case. |
| `AYVU-UI-002` | `--mode common|developer` | Selects wording/detail and guided-vs-command presentation; it must not change domain results. |
| `AYVU-UI-003` | `cache` command group | Navigation container for cache use cases; it has no independent application operation. |
| `AYVU-UI-004` | Guided Help, Back, and Exit choices; prompts and Rich tables | Interaction/rendering only. Prompt answers must be converted into stable inputs before a service is invoked. |

## Option boundary

The committed snapshot below classifies every effective long option. Short
aliases such as `-o` are normalized to their canonical long option and inherit
the same mapping. Click's generated `--help` is recorded once as a universal
presentation option and verified at every command level:

- `domain-input`: content identity or selection, such as languages and chapters;
- `application-policy`: execution, cache, conflict, retry, or mutation policy;
- `adapter-configuration`: configuration of an external adapter, such as an HTTP
  endpoint or translation backend;
- `artifact-destination`: requested output/cache/input artifact location;
- `presentation-only`: rendering or shell interaction with no domain effect;
- `presentation-confirmation`: a CLI confirmation mechanism; the underlying
  permission to mutate is an application policy;
- `compatibility-routing`: legacy CLI routing to an existing use case.

Concrete paths are serializable application inputs today, but future interfaces
must apply their own filesystem authorization before invoking a service. Likewise,
retry/rate limits are stable policies even though the current CLI provides their
defaults. `--mode`, help/completion, prompt wording, tables, colors, and exit codes
remain presentation concerns.

<!-- AYVU_CLI_SURFACE_SNAPSHOT_START -->
```json
{
  "universal_options": {
    "--help": {
      "kind": "presentation-only",
      "mappings": ["AYVU-UI-004"]
    }
  },
  "root_options": {
    "--install-completion": {
      "kind": "presentation-only",
      "mappings": ["AYVU-UI-001"]
    },
    "--mode": {
      "kind": "presentation-only",
      "mappings": ["AYVU-UI-002"]
    },
    "--preview": {
      "kind": "compatibility-routing",
      "mappings": ["AYVU-UC-006"]
    },
    "--show-completion": {
      "kind": "presentation-only",
      "mappings": ["AYVU-UI-001"]
    }
  },
  "commands": {
    "apply-review": {
      "use_cases": ["AYVU-UC-009"],
      "options": {
        "--output": "artifact-destination",
        "--overwrite": "application-policy",
        "--validate-only": "application-policy"
      },
      "subcommands": {}
    },
    "cache": {
      "use_cases": ["AYVU-UI-003"],
      "options": {},
      "subcommands": {
        "clean": {
          "use_cases": ["AYVU-UC-011"],
          "options": {
            "--all": "application-policy",
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--dry-run": "application-policy",
            "--source": "domain-input",
            "--target": "domain-input",
            "--yes": "presentation-confirmation"
          }
        },
        "export": {
          "use_cases": ["AYVU-UC-012"],
          "options": {
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--overwrite": "application-policy",
            "--source": "domain-input",
            "--target": "domain-input"
          }
        },
        "import": {
          "use_cases": ["AYVU-UC-013"],
          "options": {
            "--cache": "artifact-destination",
            "--replace": "application-policy"
          }
        },
        "inspect": {
          "use_cases": ["AYVU-UC-010"],
          "options": {
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--source": "domain-input",
            "--target": "domain-input"
          }
        }
      }
    },
    "extract": {
      "use_cases": ["AYVU-UC-008"],
      "options": {
        "--output": "artifact-destination",
        "--overwrite": "application-policy"
      },
      "subcommands": {}
    },
    "inspect": {
      "use_cases": ["AYVU-UC-001"],
      "options": {},
      "subcommands": {}
    },
    "languages": {
      "use_cases": ["AYVU-UC-003"],
      "options": {
        "--requests-per-second": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--timeout": "application-policy",
        "--url": "adapter-configuration"
      },
      "subcommands": {}
    },
    "resume": {
      "use_cases": ["AYVU-UC-007"],
      "options": {
        "--target": "domain-input"
      },
      "subcommands": {}
    },
    "test-translator": {
      "use_cases": ["AYVU-UC-002"],
      "options": {
        "--requests-per-second": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--source": "domain-input",
        "--target": "domain-input",
        "--timeout": "application-policy",
        "--url": "adapter-configuration"
      },
      "subcommands": {}
    },
    "translate": {
      "use_cases": ["AYVU-UC-004", "AYVU-UC-005"],
      "option_exclusions": {
        "AYVU-UC-005": ["--missing-output", "--output", "--review-output"]
      },
      "options": {
        "--cache": "artifact-destination",
        "--cache-only": "application-policy",
        "--chapters": "domain-input",
        "--chunk-limit": "application-policy",
        "--continue-on-error": "application-policy",
        "--dry-run": "application-policy",
        "--fail-fast": "application-policy",
        "--glossary": "artifact-destination",
        "--missing-output": "artifact-destination",
        "--output": "artifact-destination",
        "--output-dir": "artifact-destination",
        "--overwrite": "application-policy",
        "--performance-profile": "application-policy",
        "--profile": "domain-input",
        "--requests-per-second": "application-policy",
        "--require-full-cache": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--review-output": "artifact-destination",
        "--source": "domain-input",
        "--target": "domain-input",
        "--timeout": "application-policy",
        "--tm-apply-threshold": "application-policy",
        "--tm-suggest-threshold": "application-policy",
        "--translate-alt-text": "application-policy",
        "--translate-metadata": "application-policy",
        "--translation-memory": "application-policy",
        "--no-translation-memory": "application-policy",
        "--translator": "adapter-configuration",
        "--url": "adapter-configuration",
        "--workers": "application-policy"
      },
      "subcommands": {}
    }
  }
}
```
<!-- AYVU_CLI_SURFACE_SNAPSHOT_END -->

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
3. Network access is explicit per row. “Local-first” currently means a configured
   LibreTranslate-compatible HTTP endpoint, not an enforced loopback-only or
   offline boundary. Only cache-only translation guarantees no translator call.
4. Progress is currently callback-to-Rich behavior in translation. A stable event
   model is still missing for Desktop/API/MCP.
5. Cancellation is process interruption, not cooperative cancellation. No future
   interface should infer a cancellation guarantee from `KeyboardInterrupt`.
6. Resume state is a local checkpoint coordinated with cache and input identity;
   it is not durable queued-job state. Extraction must preserve compatibility
   before extending it.
7. Diagnostics currently consist of `AYVU-UC-002`, `AYVU-UC-003`, preflight
   results, and interface-formatted errors. A unified diagnostics use case does
   not yet exist.
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
test in [`test_shared_use_case_matrix.py`](../../tests/test_shared_use_case_matrix.py)
compares this snapshot with the effective Typer command tree and validates its
local references. Guided-flow changes require a manual update to the guided table
until those choices have a separately inspectable registry.
