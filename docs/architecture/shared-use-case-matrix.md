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
- **Cancellation** distinguishes the current `KeyboardInterrupt` handling from a
  future cooperative cancellation contract. No current use case offers a
  reusable cancellation token.

## Use cases

Stable IDs are permanent references. Renaming a command must not silently rename
the capability it exposes.

| ID | Capability and invocations | Current owner and evidence | Inputs | Side effects and network | Progress, cancellation, errors, artifacts | Consumers |
| --- | --- | --- | --- | --- | --- | --- |
| `AYVU-UC-001` | Inspect EPUB metadata and inventory: `inspect` | [`cli.py`](../../src/ayvu/cli.py), [`epub_io.py`](../../src/ayvu/epub_io.py); [`test_cli.py`](../../tests/test_cli.py), [`test_epub_io.py`](../../tests/test_epub_io.py) | EPUB path | Reads local EPUB; no network; no persistent write | No progress or cooperative cancellation; invalid/unreadable EPUB is an expected failure; table/data is the result, no file artifact | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-002` | Probe translator health: `test-translator` | [`cli.py`](../../src/ayvu/cli.py), [`translator.py`](../../src/ayvu/translator.py); [`test_translator.py`](../../tests/test_translator.py) | Endpoint, language pair, timeout, retries, rate/backoff policy | Sends a fixed sample to the configured HTTP translator; no local artifact | No progress or cooperative cancellation; translator failures are expected outcomes; translated probe text is the result | CLI now; Desktop diagnostics/API/MCP after extraction |
| `AYVU-UC-003` | Discover translator languages: `languages` | [`cli.py`](../../src/ayvu/cli.py), [`translator.py`](../../src/ayvu/translator.py); [`test_translator.py`](../../tests/test_translator.py) | Endpoint, timeout, retries, rate/backoff policy | Reads the configured HTTP translator; no persistent write | No progress or cooperative cancellation; transport/protocol failure is an expected outcome; language collection is the result | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-004` | Translate one EPUB: `translate <epub>`, guided Translate | [`cli.py`](../../src/ayvu/cli.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py), [`cache.py`](../../src/ayvu/cache.py), [`preflight.py`](../../src/ayvu/preflight.py), [`translator.py`](../../src/ayvu/translator.py), [`resume.py`](../../src/ayvu/resume.py), [`translation_memory.py`](../../src/ayvu/translation_memory.py), [`review_export.py`](../../src/ayvu/review_export.py), [`validation.py`](../../src/ayvu/validation.py), [`config.py`](../../src/ayvu/config.py), [`cli_progress.py`](../../src/ayvu/cli_progress.py); [`test_workflow_characterization.py`](../../tests/test_workflow_characterization.py), [`test_cli_progress.py`](../../tests/test_cli_progress.py) | Input EPUB, language/profile, chapter selection, translator/cache/glossary/TM policies, execution controls, output/review policies | Reads the original without changing it; reads/writes SQLite cache and checkpoint; normally calls configured HTTP translator; `--cache-only` makes no translator request; may write translated EPUB, review CSV, missing-text TXT, and Markdown reports | Chapter/segment progress exists only through CLI callbacks; `KeyboardInterrupt` preserves resumable state but is not cooperative cancellation; validation, preflight, per-segment, and output conflicts are structured only partially today | CLI now; Desktop/API/MCP after service extraction |
| `AYVU-UC-005` | Translate an EPUB batch: `translate <epub>...` | [`cli.py`](../../src/ayvu/cli.py); [`test_cli.py`](../../tests/test_cli.py), [`test_workflow_characterization.py`](../../tests/test_workflow_characterization.py) | Ordered input paths, output directory, language/profile, chapter selection, translator/cache/glossary/TM and execution policies, plus continue/fail-fast policy. Multiple inputs reject `--output` and `--review-output`; `--missing-output` is accepted by the current CLI but its explicit path is not forwarded to batch items | Runs the core translation effects per input. It may write one translated EPUB and one Markdown report per item; cache-only misses produce an automatically named missing-text TXT in the reports directory, not the path requested through `--missing-output`; no review CSV is produced by batch today | Per-book and inner translation progress are CLI-owned today; interruption is process-level; result is a batch summary plus per-book EPUB/report paths or errors | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-006` | Build a translation preview: root `--preview`, guided Preview | [`cli.py`](../../src/ayvu/cli.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py); [`test_cli.py`](../../tests/test_cli.py) | EPUB path, target language, current configured defaults | Reads the original; may use cache and configured HTTP translator; writes a new preview EPUB and never mutates the original | CLI progress/errors only; no cooperative cancellation contract; preview EPUB is the artifact | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-007` | Resume an interrupted translation: `resume [epub]` | [`cli.py`](../../src/ayvu/cli.py), [`resume.py`](../../src/ayvu/resume.py); [`test_resume.py`](../../tests/test_resume.py), [`test_cli.py`](../../tests/test_cli.py) | Optional EPUB selector and target language; saved checkpoint supplies remaining inputs | Reads/writes checkpoint and cache, may call the saved HTTP translator, and writes the planned output | Resumes chapter-level progress; `KeyboardInterrupt` may preserve state; corrupt/ambiguous/missing checkpoint and changed input are failures; checkpoint is file-backed state, not a durable job/service contract | CLI now; future consumers only after durable state and cancellation contracts are designed |
| `AYVU-UC-008` | Extract reader-visible content: `extract` | [`cli.py`](../../src/ayvu/cli.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py); [`test_cli.py`](../../tests/test_cli.py), [`test_html_translate.py`](../../tests/test_html_translate.py) | EPUB path, output directory, overwrite policy | Reads EPUB and writes extracted local files; no network | No progress or cooperative cancellation; invalid EPUB/path/conflict are expected failures; extracted directory is the artifact | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-009` | Inspect legacy review or validate a v2 package: `apply-review` remains fail-closed | [`review_import.py`](../../src/ayvu/review_import.py), [`review_exchange.py`](../../src/ayvu/review_exchange.py), [`cli.py`](../../src/ayvu/cli.py); [`test_review_exchange.py`](../../tests/test_review_exchange.py), [`test_cli.py`](../../tests/test_cli.py) | Bounded v1 inspection file or exact project/source/machine/format context for v2 mapping | V1 inspection is read-only; v2 parse/map has no persistence, network, reconstruction or publication side effects; CLI apply returns `legacy_reexport_required` for v1 | Mapping returns structural inputs only; CLI v2 export/apply and final EPUB publication are not connected | CLI keeps the fail-closed path; future callers can use the v2 library contract |
| `AYVU-UC-010` | Inspect cache: `cache inspect` | [`cli.py`](../../src/ayvu/cli.py), [`cache.py`](../../src/ayvu/cache.py); [`test_cache.py`](../../tests/test_cache.py), [`test_cli.py`](../../tests/test_cli.py) | Cache path and optional language/date filters | Reads SQLite cache; opening may initialize the database; no network | No progress/cancellation; invalid date, path, or database is a failure; summary data is the result | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-011` | Clean cache: `cache clean` | [`cli.py`](../../src/ayvu/cli.py), [`cache.py`](../../src/ayvu/cache.py); [`test_cache.py`](../../tests/test_cache.py), [`test_cli.py`](../../tests/test_cli.py) | Cache path, filters/all selector, dry-run policy; confirmation is presentation | Counts or deletes SQLite rows; no network | No progress/cancellation; unsafe selector combinations are rejected; deletion/count report is the result | CLI now; Desktop/API/MCP after extraction with authorization appropriate to each interface |
| `AYVU-UC-012` | Export cache: `cache export` | [`cli.py`](../../src/ayvu/cli.py), [`cache.py`](../../src/ayvu/cache.py); [`test_cache.py`](../../tests/test_cache.py), [`test_cli.py`](../../tests/test_cli.py) | Cache path, filters, JSON destination, overwrite policy | Reads SQLite and writes JSON; no network | No progress/cancellation; path/database errors are failures; JSON export is the artifact | CLI now; Desktop/API/MCP after extraction |
| `AYVU-UC-013` | Import cache: `cache import` | [`cli.py`](../../src/ayvu/cli.py), [`cache.py`](../../src/ayvu/cache.py); [`test_cache.py`](../../tests/test_cache.py), [`test_cli.py`](../../tests/test_cli.py) | Ayvu JSON export, cache path, replace policy | Reads JSON and writes SQLite; no network | No progress/cancellation; schema/path/database errors are failures; import report is the result | CLI now; Desktop/API/MCP after extraction with explicit mutation authorization |
| `AYVU-UC-014` | Discover, inspect, and open library books: guided Library | [`cli.py`](../../src/ayvu/cli.py), [`library.py`](../../src/ayvu/library.py); [`test_library.py`](../../tests/test_library.py), [`test_cli.py`](../../tests/test_cli.py) | Configured library paths, selected book/action, optional reader application | Scans local directories and may launch a local reader process; no network owned by Ayvu | No progress/cancellation; scan/open errors are outcomes; book inventory is data and the existing EPUB remains the artifact | Guided CLI now; Desktop after extraction; API/MCP must not inherit process-launch behavior implicitly |
| `AYVU-UC-015` | Read and update preferences: guided Settings; configuration consumed by other flows | [`cli.py`](../../src/ayvu/cli.py), [`config.py`](../../src/ayvu/config.py); [`test_config.py`](../../tests/test_config.py), [`test_cli.py`](../../tests/test_cli.py) | Default target, books directory, feature folder names, reader command, existing profiles | Reads/writes local configuration; no network | No progress/cancellation; validation/write failures are outcomes; configuration file is the artifact | Guided CLI now; Desktop after extraction; API/MCP require a separately authorized configuration boundary |
| `AYVU-UC-016` | Create, edit, preview, and select glossaries: guided Glossaries and translation input | [`cli.py`](../../src/ayvu/cli.py), [`glossary.py`](../../src/ayvu/glossary.py); [`test_glossary.py`](../../tests/test_glossary.py), [`test_cli.py`](../../tests/test_cli.py) | Glossary path, ordered terms, preserve/translate rule, preferred translation | Reads/writes local glossary JSON; no network | No progress/cancellation; invalid JSON/rules and write errors are outcomes; glossary JSON is the artifact | Guided CLI now; Desktop after extraction; API/MCP require explicit filesystem authorization |

## Proposed target ownership

These are logical module boundaries for the extraction, not accepted package
structure. They remain `Proposed` until human review, and they do not imply that
the named modules exist today. An application module orchestrates each use case;
ports describe required capabilities; concrete EPUB, SQLite, filesystem, process,
and HTTP implementations remain adapters.

| Use-case IDs | Proposed application owner | Required ports/adapters |
| --- | --- | --- |
| `AYVU-UC-001` | `ayvu.application.inspection` | EPUB reader/inventory port |
| `AYVU-UC-002`, `AYVU-UC-003` | `ayvu.application.diagnostics` | Translator capability/health port governed by network policy |
| `AYVU-UC-004`, `AYVU-UC-005` | `ayvu.application.translation` | EPUB transformer, translator, cache, checkpoint, glossary, TM, validation, progress-event, and artifact ports |
| `AYVU-UC-006` | `ayvu.application.preview` | Translation core, cache, translator, validation, and preview artifact ports |
| `AYVU-UC-007` | `ayvu.application.resume` | Checkpoint, input-identity, cache, translator, progress-event, and output ports |
| `AYVU-UC-008` | `ayvu.application.extraction` | EPUB visible-content reader and artifact-writer ports |
| `AYVU-UC-009` | `ayvu.application.review` | V2 review-package reader/mapper, EPUB reconstruction and output ports; legacy v1 inspection remains read-only |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-012`, `AYVU-UC-013` | `ayvu.application.cache` | Cache query/mutation and import/export artifact ports |
| `AYVU-UC-014` | `ayvu.application.library` | Library catalog and explicitly authorized local opener ports |
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
        "--overwrite": "application-policy"
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
