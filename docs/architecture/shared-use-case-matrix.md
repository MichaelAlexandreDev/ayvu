# Shared application use-case matrix

Status: matriz de implementação consolidada em #158; não altera o estado dos ADRs.

Esta é a matriz compartilhada de #123, atualizada após as dependências de #158.
Registra os serviços entregues e as decisões de apresentação da CLI. Desktop,
API e MCP devem consumir esses contratos por suas próprias raízes de composição;
a matriz não introduz uma interface nem aceita um ADR adicional.

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
| `AYVU-UC-002` | Teste explícito: `test-translator` | `provider_discovery.ProviderDiscoveryService.test_provider`, via `cli_composition.cli_provider_session`; `test_provider_discovery.py`, `test_cli.py` | Configuração explícita, par de idiomas e política de execução | Envia somente a amostra pública fixa; nenhum documento | Resultado tipado, diagnóstico limitado e saída legada | CLI e consumidor sem framework; futuras interfaces autorizam sua política |
| `AYVU-UC-003` | Idiomas: `languages` e seleção guiada | `provider_discovery.ProviderDiscoveryService.languages`, via `cli_composition.cli_provider_session`; `test_provider_discovery.py`, `test_preflight.py`, `test_cli.py` | Configuração explícita e política de execução | Consulta explícita de idiomas; não realiza tradução de teste | Coleção tipada, falhas normalizadas e renderização CLI | CLI e consumidor sem framework |
| `AYVU-UC-004` | Translate one EPUB: `translate <epub>`, guided Translate | [`cli.py`](../../src/ayvu/cli.py), [`translate_project.py`](../../src/ayvu/translate_project.py), [`review_application.py`](../../src/ayvu/review_application.py), [`review_composition.py`](../../src/ayvu/review_composition.py), [`epub_io.py`](../../src/ayvu/epub_io.py), [`html_translate.py`](../../src/ayvu/html_translate.py), [`cache.py`](../../src/ayvu/cache.py), [`preflight.py`](../../src/ayvu/preflight.py), [`translator.py`](../../src/ayvu/translator.py), [`resume.py`](../../src/ayvu/resume.py), [`translation_memory.py`](../../src/ayvu/translation_memory.py), [`validation.py`](../../src/ayvu/validation.py), [`config.py`](../../src/ayvu/config.py), [`cli_progress.py`](../../src/ayvu/cli_progress.py); [`test_workflow_characterization.py`](../../tests/test_workflow_characterization.py), [`test_cli_translate_project.py`](../../tests/test_cli_translate_project.py), [`test_cli_progress.py`](../../tests/test_cli_progress.py) | Input EPUB, language/profile, chapter selection, translator/cache/glossary/TM policies, execution controls, output/review policies | Reads the original without changing it; reads/writes SQLite cache and checkpoint; normally calls configured HTTP translator; `--cache-only` makes no translator request; may write translated EPUB, v2 review package, missing-text TXT, and Markdown reports | `TranslateProject` owns the durable translation result; review export consumes that exact result through a separate no-provider use case and D106 publication; cancellation and outcome contracts are shared | CLI now; framework-free application consumers now; Desktop/API/MCP after adapter work |
| `AYVU-UC-005` | Lote: `translate <epub>...` | `translate_batch.TranslateBatch`, `translate_batch_composition.LocalTranslateBatch`, via `cli_composition.translate_batch`; `test_translate_batch_integration.py`, `test_batch_bounded_validation.py` | Ordem, destinos, políticas e confirmações resolvidas; mesmas restrições públicas | Plano completo e limitado antes da execução; executa o mesmo serviço por projeto; publicações e relatórios por item | Eventos e resultados por item; cancelamento, continuidade e falha rápida pertencem ao serviço; Rich apenas projeta | CLI e consumidor sem framework |
| `AYVU-UC-006` | Preview: raiz `--preview` e fluxo guiado | `cli_composition._translate_without_project` conecta a fachada entregue `translate_project_composition.translate_without_project`; `test_workflow_characterization.py`, `test_cli.py`, `test_cli_composition.py` | EPUB, destino confirmado, idioma e seleção de preview | Lê original; usa somente cache/provedor necessários; publica preview sem criar projeto/checkpoint | Mantém os callbacks de compatibilidade e uma validação final; relatório e caminho visível deliberadamente | CLI; futura interface deve fornecer seu próprio presenter |
| `AYVU-UC-007` | Retomada: `resume [epub]` e oferta guiada | `job_recovery.ListInterruptedJobs`, `InspectJobRecoverability`, `RequestJobResume`, via `cli_composition.open_job_recovery`; `test_cli_recovery.py`, `test_job_recovery_integration.py` | Referência opaca, decisões explícitas e configuração salva | Inspeção somente leitura; retomada autoriza continuação do mesmo `TranslateProject`; checkpoint legado continua compatível | Resultado de recuperabilidade, motivos limitados, cancelamento e cadeia durável; menu e confirmação pertencem à CLI | CLI e consumidor sem framework |
| `AYVU-UC-008` | Extract reader-visible content: `extract` | [`content_use_cases.py`](../../src/ayvu/content_use_cases.py), [`content_composition.py`](../../src/ayvu/content_composition.py), [`epub_format.py`](../../src/ayvu/epub_format.py), [`html_structure.py`](../../src/ayvu/html_structure.py), [`markdown_writer.py`](../../src/ayvu/markdown_writer.py); [`test_content_use_cases.py`](../../tests/test_content_use_cases.py), [`test_cli.py`](../../tests/test_cli.py) | EPUB source reference, authorized output directory and overwrite policy; optional expected revision, cancellation and progress ports | Reads EPUB and atomically publishes one Markdown file per document; no network or durable project/artifact record; unrelated output files are preserved | Full output plan is validated before publication; typed per-file completed/failed/not-started outcomes, progress, cancellation and normalized failures; partial publication is explicit and no directory transaction is claimed | CLI and framework-free consumer now; Desktop/API/MCP after adapter work |
| `AYVU-UC-009` | Export, inspect, validate, and apply review packages: `translate --review-output`, `apply-review` | [`review_application.py`](../../src/ayvu/review_application.py), [`review_composition.py`](../../src/ayvu/review_composition.py), [`review_import.py`](../../src/ayvu/review_import.py), [`review_exchange.py`](../../src/ayvu/review_exchange.py), [`cli.py`](../../src/ayvu/cli.py); [`test_review_application.py`](../../tests/test_review_application.py), [`test_review_exchange.py`](../../tests/test_review_exchange.py), [`test_cli_translate_project.py`](../../tests/test_cli_translate_project.py), [`test_cli.py`](../../tests/test_cli.py) | Exact `TranslateProjectResult` for export; bounded v1 inspection or v2 bytes plus source/project/machine/format context; output and overwrite policy for apply | V1 inspection and v2 validation are read-only; export/apply use no provider/network; export publishes the sensitive v2 package through D106; apply reconstructs, validates and publishes a new EPUB through D106 | Application commands/results are framework-neutral; validation evidence is a digest, not authorization; apply fully revalidates; v1 apply stops with `legacy_reexport_required`; job, progress, cancellation, errors, events and provenance carry bounded codes and opaque identities | CLI and framework-free application consumer now; future Desktop/API/MCP after adapter work |
| `AYVU-UC-010` | Inspect cache | [`cache_admin.py`](../../src/ayvu/cache_admin.py), `cache_admin_composition.py`, `cache_admin_v2_store.py`, [`cli.py`](../../src/ayvu/cli.py) | Cache reference, authorized namespace or explicit all, filters | Reads SQLite without creating absent caches; no network | Bounded aggregate metadata, progress, cancellation, warnings and normalized failures; default legacy gate remains | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-011` | Preview and clean cache | Same as UC-010 | Cache reference, authorized namespace, filters or explicit all; interface confirmation | Transactional deletion after preview revalidation; no network | Per-namespace aggregate counts, progress, cancellation, warnings and stale-preview failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-012` | Export cache | Same as UC-010 | Cache and destination references, authorized namespace or explicit all, filters and overwrite policy | Reads SQLite and atomically publishes sensitive JSON; no network | Count and actual JSON format version, progress, cancellation, warnings and normalized failures | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-013` | Import cache | Same as UC-010 | Cache and source references, caller-authorized external scope map, replace policy and revision-bound preview | Validates JSON and mapping before transactional SQLite mutation; no network; preview is discarded on refusal/failure or retained as audit after successful import | Counts, conflicts, duplicates, preview revision, progress, cancellation, warnings and normalized failures; sensitive audit artifact | CLI and framework-free consumers with appropriate authorization |
| `AYVU-UC-014` | Discover, inspect, and open library books: guided Library | [`cli.py`](../../src/ayvu/cli.py), [`library_service.py`](../../src/ayvu/library_service.py), [`library_adapters.py`](../../src/ayvu/library_adapters.py) | Configured library root and folder names, opaque book/entry references, optional reader command | Discovery reads local directory metadata without opening EPUB contents; an explicit open can launch a local reader | Grouped entries, bounded warnings, cooperative cancellation and normalized open outcomes; a new scan invalidates prior references | Guided CLI and framework-free service consumers; other interfaces must authorize reader launch explicitly |
| `AYVU-UC-015` | Preferências: primeiro uso, Settings e leitura de configuração | `preferences_service.get_preferences` / `update_preferences`, via `cli_composition.configuration_store`; fachada `config.ConfigStore`, valores `config_values`; `test_preferences_service.py`, `test_preferences_store.py`, `test_config.py`, `test_cli.py` | Valores v1 e revisão esperada; prompts resolvidos | Repositório JSON selecionado; leitura não cria configuração; escrita valida revisão | Snapshot, avisos, recusa e resultado de persistência; apresentação conserva mensagens e menus | CLI e consumidor sem framework |
| `AYVU-UC-016` | Create, edit, preview, and select managed glossaries | [`glossary_catalog.py`](../../src/ayvu/glossary_catalog.py), [`glossary_catalog_store.py`](../../src/ayvu/glossary_catalog_store.py), [`glossary_catalog_composition.py`](../../src/ayvu/glossary_catalog_composition.py), [`cli.py`](../../src/ayvu/cli.py) | Trusted managed root, opaque references, entries and revisions | Bounded local JSON catalog; no network | Normalized failures and cooperative cancellation; divergent updates are unavailable; presentation owns prompts and paths | Guided CLI and framework-free consumers with a supplied repository |

## Proposed target ownership

O título desta seção permanece estável para os testes de #123. As antigas
propostas abaixo agora apontam para os contratos efetivamente entregues.

| Use-case IDs | Application owner entregue | Portas/adaptadores na composição |
| --- | --- | --- |
| `AYVU-UC-001`, `AYVU-UC-008` | `content_use_cases.InspectContent`, `ExportVisibleContent` | `content_composition`: formatos e publicação Markdown |
| `AYVU-UC-002`, `AYVU-UC-003` | `provider_discovery.ProviderDiscoveryService` | `provider_composition`: descoberta, teste e preflight explícitos |
| `AYVU-UC-004` | `translate_project.TranslateProject` | `translate_project_composition`: execução, formato, cache, validação e publicação |
| `AYVU-UC-005` | `translate_batch.TranslateBatch` | `translate_batch_composition`: plano limitado e itens do mesmo serviço |
| `AYVU-UC-006` | Fachada de compatibilidade de preview entregue por #145 | `cli_composition` conecta `translate_without_project`; não cria um segundo serviço |
| `AYVU-UC-007` | `job_recovery` e o mesmo `TranslateProject` | Evidências somente leitura e checkpoint legado via composição |
| `AYVU-UC-009` | `review_application` | `review_composition`: codec, reconstrução e publicação D106 |
| `AYVU-UC-010`–`AYVU-UC-013` | `cache_admin` | `cache_admin_composition`: repositórios por namespace e intercâmbio |
| `AYVU-UC-014` | `library_service.LibraryService` | `library_adapters`: leitura local e abertura explicitamente solicitada |
| `AYVU-UC-015` | `preferences_service` | `config.ConfigStore` sobre `JsonPreferencesRepository`; valores em `config_values` |
| `AYVU-UC-016` | `glossary_catalog` | `glossary_catalog_composition`: catálogo JSON gerenciado |

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
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
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
            "--all": "application-policy",
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
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
            "--all": "application-policy",
            "--dry-run": "application-policy",
            "--scope-map": "artifact-destination",
            "--yes": "presentation-confirmation",
            "--cache": "artifact-destination",
            "--replace": "application-policy"
          }
        },
        "inspect": {
          "use_cases": ["AYVU-UC-010"],
          "options": {
            "--all": "application-policy",
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
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
4. Jobs e casos de uso fornecem resultados e eventos estruturados. A tradução
   mantém também os callbacks entregues de capítulo/texto/validação para o
   progresso Rich legado; esses callbacks não inventam estado de job ou artefato.
5. Cancelamento cooperativo e resultados terminais são dos serviços; a CLI
   converte também a interrupção do processo para o significado de saída legado.
6. O checkpoint legado permanece uma fachada viva, coordenada com a recuperação
   durável por `job_recovery`; removê-lo quebraria arquivos de retomada existentes.
7. Diagnósticos de provedor usam o mesmo `ProviderDiscoveryService` que consumidores
   sem framework. Isso não cria um serviço universal de diagnóstico nem uma
   nova política de rede.
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

## Composição final da CLI (#158)

A entrada instalada continua `ayvu = ayvu.cli:app`. `cli.py` e `cli_progress.py`
tratam Typer, modo comum/developer, perguntas, confirmações, Rich, caminhos
selecionados, relatórios textuais e códigos de saída. Importam contratos/resultados
e funções nomeadas de `cli_composition.py`; não importam os adaptadores concretos.
`cli_contracts.py` contém portas estruturais e erros compartilhados sem Typer/Rich.
`config_values.py` separa valores de preferências da fachada persistente.

`cli_composition.py` é a única fronteira de construção escolhida pela CLI.
Suas funções nomeadas compõem somente o fluxo solicitado usando os módulos de
composição predecessores. Não há registro de serviços, singleton, cache global
de instâncias ou descoberta dinâmica. As dependências são passadas aos serviços
por suas portas e configuração explícita; resultados, avisos e identidade do job
são devolvidos sem fabricar sucesso. Desktop/API/MCP usam seus próprios roots
e os mesmos serviços; não devem importar `cli.py` ou reutilizar objetos Rich.

Importar a CLI e pedir qualquer ajuda não abre repositórios nem cria sessão de
provedor. Inspect abre somente a origem; a inspeção de cache usa as consultas
somente leitura do adaptador existente. Configuração ausente não é criada pela
leitura. Cache-only não constrói provedor/sessão, não resolve segredos e não faz
DNS/socket/HTTP: a validação pura de configuração conserva as recusas anteriores.
O cache selecionado e, numa tradução durável, o projeto selecionado continuam
necessários; nenhum armazenamento alheio é aberto ou migrado. Um checkpoint
novo permanece vazio até o ponto seguro de preflight de uma tradução online.

A política de rede legada continua explicitamente `UNCLASSIFIED`; nenhuma URL
é usada para inferir autoridade. A remoção da sondagem de tradução oculta por
#128 permanece a correção de segurança já aprovada. #158 não adiciona exceção
observável. Publicação segura, imutabilidade da origem e regras de cache/glossário
continuam nos serviços/adaptadores entregues. Relatórios CLI conservam escrita,
modo de arquivo e nomes anteriores, sem introduzir lock, hard link ou novo limite.

O contrato normalizado está congelado em `tests/fixtures/cli-post-predecessor.json`,
capturado no SHA `3edd2c2813c592418189670dbef1866c3026780e` antes da refatoração.
Inclui árvore, aliases, parâmetros, requiredness, defaults, tipos, ambiente e ajuda.
A caracterização de #132, `test_workflow_characterization.py`, permanece intacta;
os testes CLI antigos conservam suas asserções e apenas apontam canários para
os novos donos. Alterar simultaneamente o comportamento e esse baseline exige
decisão de compatibilidade documentada e revisão separada.

## CLI helper and facade inventory

Inventário de todos os símbolos definidos pela apresentação, verificado pelo
mesmo teste da matriz de #123. Cada grupo abaixo referencia as linhas de serviço
ou ação de apresentação acima. Funções que executam IO delegam à composição;
renderizar e selecionar caminhos continua sendo responsabilidade da apresentação.

| Mapeamento | Símbolos atuais de `cli.py` |
| --- | --- |
| `AYVU-UI-004` | `_RouteView`, `TranslationRunResult`, `BatchTranslationResult`, `_BatchItemExit`, `ExecutionControls` |
| `AYVU-UI-004` | `main` |
| `AYVU-UC-001` | `inspect` |
| `AYVU-UC-002` | `test_translator` |
| `AYVU-UC-003` | `languages` |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-012`, `AYVU-UC-013` | `_cache_namespace`, `_cache_scope_map`, `_cache_warnings`, `cache_inspect`, `cache_clean`, `_cache_failure_hint`, `cache_export`, `cache_import` |
| `AYVU-UC-004`, `AYVU-UC-005` | `translate` |
| `AYVU-UC-007` | `resume` |
| `AYVU-UI-004` | `_print_expected_error` |
| `AYVU-UC-010`, `AYVU-UC-011`, `AYVU-UC-013` | `_normalize_cache_before`, `_validate_cache_clean_options`, `_print_cache_summary`, `_print_cache_import_report`, `_plural_cache_entries` |
| `AYVU-UC-001`, `AYVU-UC-008` | `_print_epub_read_error`, `_print_content_failure` |
| `AYVU-UC-004`, `AYVU-UC-005`, `AYVU-UC-006` | `_resolve_source_language`, `_print_translation_plan`, `_execution_control_rows`, `_format_workers`, `_format_rate_limit`, `_format_retry_policy`, `_format_execution_summary`, `_format_number`, `_resolve_execution_controls`, `_resolve_performance_profile`, `_print_translation_route`, `_resolve_requested_profile`, `_resolve_target_language`, `_resolve_glossary_path`, `_parse_chapter_selection`, `_resolve_chapter_selection_for_display`, `_print_selected_chapters`, `_validate_translate_command_options`, `_validate_cache_only_options`, `_build_translation_memory_options`, `_validate_worker_options`, `_resolve_translation_output_dir`, `_run_translation`, `_translation_progress_display`, `_print_project_failure`, `_print_translation_failure` |
| `AYVU-UC-005` | `_run_batch_translation`, `_print_batch_plan`, `_print_batch_summary` |
| `AYVU-UC-006` | `_run_preview` |
| `AYVU-UC-008` | `extract` |
| `AYVU-UC-009` | `apply_review`, `_print_review_apply_failure` |
| `AYVU-UC-004`, `AYVU-UC-006`, `AYVU-UC-009` | `_validate_with_progress`, `_print_validation_warnings`, `_print_report`, `_has_glossary_summary`, `_print_glossary_warnings`, `_resolve_review_output_path`, `_print_review_export_failure` |
| `AYVU-UC-007` | `_print_interrupted_translation`, `_partial_chapter_value`, `_interrupted_output_message`, `_print_processing_translation_states` |
| `AYVU-UI-004` | `_run_guided_main_flow` |
| `AYVU-UC-015` | `_load_or_init_config`, `_load_existing_config_or_default`, `_init_default_language_config`, `_choose_initial_folder_names`, `_print_feature_folder_paths`, `_save_config` |
| `AYVU-UI-004` | `_print_guided_main_menu`, `_handle_guided_main_choice` |
| `AYVU-UC-004` | `_run_guided_translation` |
| `AYVU-UC-006` | `_run_guided_preview` |
| `AYVU-UC-004` | `_choose_guided_translation_profile`, `_print_translation_profiles`, `_choose_guided_profile_glossary` |
| `AYVU-UC-016` | `_run_guided_glossaries`, `_print_guided_glossary_menu`, `_create_guided_glossary`, `_edit_guided_glossary`, `_preview_guided_glossary`, `_choose_guided_translation_glossary`, `_prompt_new_glossary_path`, `_prompt_guided_glossary_terms`, `_prompt_guided_glossary_entry`, `_prompt_required_text`, `_upsert_glossary_entry`, `_save_guided_glossary`, `_glossary_failure`, `_load_guided_glossary`, `_prompt_saved_glossary_path`, `_print_saved_glossaries`, `_print_guided_glossary_preview`, `_list_glossary_files` |
| `AYVU-UC-014` | `_run_guided_library`, `_print_library_books`, `_prompt_library_book`, `_print_library_warnings`, `_run_guided_library_book`, `_library_book_actions`, `_print_library_book_actions`, `_print_library_book_info`, `_open_library_book_path`, `_display_book_translations` |
| `AYVU-UC-015` | `_run_guided_settings`, `_print_settings`, `_print_settings_menu`, `_update_guided_settings`, `_settings_with_default_language`, `_settings_with_books_dir`, `_settings_with_folder_names`, `_settings_with_reader_app`, `_format_profile_names` |
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
| `AYVU-UC-007` | `_checkpoint_action` |
| `AYVU-UC-004`, `AYVU-UC-005` | `_report_stem_for`, `_missing_stem_for` |

A única função removida, `_count_cache_entries`, não tinha chamadas, registros
Typer ou consumidores de compatibilidade; o inventário de imports/chamadas e a
suíte cobrem sua retirada. Os helpers de execução/checkpoint/publicação foram
movidos, não descartados. As fachadas `config`, `resume`, `preflight`,
`provider_composition`, `batch_output_policy` e `translate_project_composition`
continuam reexportando os mesmos valores/erros para consumidores existentes.
Os delegates privados `_translate_in_project` e `_translate_without_project`
permanecem na CLI por seus consumidores de caracterização; não contêm negócios.
