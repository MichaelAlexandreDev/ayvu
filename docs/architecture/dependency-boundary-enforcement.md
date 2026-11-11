# Static architectural dependency checks

The [accepted modular-monolith ADR](modular-monolith-boundaries.md) is enforced
by [the architecture tests](../../tests/test_architecture_boundaries.py), added
for [#133](https://github.com/DevStarrk1137/ayvu/issues/133). This enforcement
does not accept other proposed decisions or change production behavior.

Run the focused checks with:

```bash
uv run pytest tests/test_architecture_boundaries.py
uv run pytest
git diff --check
```

## Classification and rules

`OWNERS` lists every production module by exact module name, logical layer and
capability. `ALLOWED_INTERNAL_EDGES` separately lists every reviewed import
between production modules. Classification is reviewed against behavior and ADR
ownership; directory names do not confer permission. New modules, including
package initializers, require explicit classification and every new internal
edge requires an exact allowlist entry. Missing modules and stale allowlist
entries fail, so a rename cannot silently discard protection.

| Modules | Layer | Capability |
| --- | --- | --- |
| `ayvu` | Independent package surface | Package metadata |
| `domain`, `chunking` | Domain | Shared values, Translation |
| `project` | Domain | Project identity and immutable manifest values |
| `projects.service` | Application | Project repository port and resolution policy |
| `projects.store` | Infrastructure | Atomic local manifest storage and source fingerprinting |
| `preferences` | Domain | Immutable v1 preference/profile values and validation |
| `preferences.service` | Application | Shared get/update requests, outcomes and repository port |
| `preferences.store` | Infrastructure | Bounded JSON, expected revisions and atomic publication |
| `migration_contracts` | Domain | Content-free schema version plans, errors and outcomes |
| `sqlite_migrations` | Infrastructure | SQLite bootstrap, verified backups and transactional migration runner |
| `projects.schema` | Infrastructure | Production migration registry and explicit job, artifact and provenance bootstrap |
| `job` | Domain | Job values, transition table, lease rules and typed errors |
| `jobs.service` | Application | `JobRepository` port and job snapshot |
| `jobs.store` | Infrastructure | SQLite job adapter and job schema SQL |
| `jobs.events` | Domain | Job events, outcomes, ordering rules and canonical serialization |
| `jobs.execution` | Application | Event ports, bounded in-memory log and cooperative cancellation |
| `v1.resume_scan` | Infrastructure | Read-only view of a legacy checkpoint |
| `artifact` | Domain | Artifact identity, lifecycle, derived locations and reuse predicate |
| `artifacts.service` | Application | Artifact ports, publication order, reuse and reconciliation |
| `artifacts.store` | Infrastructure | SQLite artifact records and contained local payload files |
| `content` | Domain | Content documents, translation units, deterministic identity and canonical serialization |
| `provenance` | Domain | Lineage record values, explicit unknowns and canonical serialization |
| `provenance.service` | Application | Provenance repository port and query results |
| `provenance.store` | Infrastructure | Append-only SQLite lineage and bounded traversal |
| `knowledge.memory.fuzzy` | Transitional domain | Knowledge/TM |
| `library` | Transitional application | Legacy library compatibility |
| `library.service` | Application | Library discovery and authorized open coordination |
| `library.adapters` | Infrastructure | Local library scanning and reader launch |
| `preflight` | Composition | Translation |
| `cli`, `interfaces.cli.progress` | Interface | CLI |
| `composition.cli` | Composition | Fronteira única de construção escolhida pela CLI (#158) |
| `interfaces.cli.contracts` | Application | Portas estruturais e erros compartilhados, sem framework |
| `preferences.model` | Domain | Valores de preferências e conversões da fachada v1 |
| `formats.epub.archive`, `formats.epub.adapter`, `formats.epub.io`, `formats.epub.html_structure`, `v1.html_translate`, `v1.translation`, `validation` | Format adapter | EPUB, EPUB/Quality |
| `translation.engine` | Application | One book through a format adapter and a unit executor |
| `translation.project` | Application | Project admission, reconciliation and provenance, from its ports |
| `translation.project_contracts` | Application | Project commands, plans, ports and results |
| `translation.project_run` | Application | The active project job through its terminal outcome |
| `translation.project_lease` | Application | Concurrent project job lease renewal |
| `recovery.service` | Application | Listing, inspecting and resuming interrupted translations |
| `recovery.store` | Infrastructure | The read-only evidence recovery decides from |
| `composition.builders.translate_project` | Composition | The current language work and validator behind those ports |
| `composition.builders.review`, `review_files`, `review_context`, `review_packages`, `review_start`, `review_operations` | Composition | Review wiring, file boundaries, context verification, packages, startup and publication/settlement |
| `artifacts.destination` | Infrastructure | Publishing a job's output to the file the user chose |
| `providers.libretranslate.client` | Provider adapter with transitional embedded port | Providers |
| `content.protection` | Domain | Protected terms, placeholders and their integrity checks |
| `translation.plan` | Domain | Resolved plan, stage identities and fingerprints |
| `translation.pipeline` | Application | Fixed linguistic stages, their ports and the legacy facade |
| `translation.document` | Application | Document admission, active unit execution, events and publication |
| `translation.document_contracts` | Application | Document contracts, safe points and protected content |
| `translation.raw_results` | Application | Canonical raw results, reuse dimensions and payload validation |
| `translation.document_replay` | Application | Proving and replaying current and ancestor raw results |
| `translation.document_provenance` | Application | Document production and replay lineage |
| `translation.document_bindings` | Application | Provider, cache and memory bindings for the current unit and plan |
| `provider` | Domain | Provider request, result, features and allowlisted diagnostics |
| `providers.capabilities` | Domain | Provider descriptors, capability snapshots, availability and negotiation |
| `formats.contracts` | Domain | What a format claims, negotiation, and the values its operations exchange |
| `formats.service` | Application | Format ports, registry and the checks before any format does work |
| `formats.local` | Infrastructure | Local files as format sources, and the check that a destination is not the source |
| `content_use_cases` | Application | Shared read-only inspect and visible-content export requests/results |
| `formats.markdown.writer` | Infrastructure | Safe Markdown planning and atomic per-file publication |
| `composition.builders.content` | Composition | Explicit format registry and local content workflow adapters |
| `providers.network` | Domain | The network decision an operation must present, and its enforcement |
| `providers.service` | Application | Provider and factory ports, registry and strict result checking |
| `providers.discovery` | Application | Shared use cases to list, describe, probe, route, test and preflight providers |
| `providers.libretranslate.adapter` | Provider adapter | LibreTranslate compatibility adapter, factory, capability report and probe |
| `composition.builders.providers` | Composition | Adaptador de montagem de provedor selecionado por `composition.cli` |
| `cache`, `glossary` | Knowledge adapter | Exact cache, Glossary |
| `knowledge.cache.admin` | Application | Typed cache administration use cases and ports |
| `knowledge.cache.admin_store` | Knowledge adapter | Standalone SQLite administration and local JSON publication |
| `knowledge.cache.admin_v2_store`, `admin_v2_queries`, `admin_v2_files`, `admin_v2_export`, `admin_v2_records`, `admin_v2_import` | Knowledge adapter | Namespace administration, SQLite views, owned files, streaming exports, records and private import staging |
| `composition.builders.cache_admin` | Composition | Local cache administration adapters |
| `config`, `resume`, `review.csv_export`, `review.csv_import` | Infrastructure | Preferences, Jobs, Review |

Application imports domain/application contracts; domain imports domain APIs.
The internal graph must be acyclic. Interface imports application/domain values
and presenters from the same interface. Format, provider, knowledge and
infrastructure adapters can import application/domain ports they implement.
Format and provider adapters cannot import one another. Knowledge adapters
cannot import interfaces, formats or infrastructure. Infrastructure cannot
import interfaces or formats. Package initializers remain independent of
functional modules to prevent reexports from hiding concrete adapters.
The layer matrix is an upper bound: it cannot authorize an import without an
exact module edge in `ALLOWED_INTERNAL_EDGES`. An allowed edge cannot override a
forbidden layer direction.

The six review builder modules may import one another only through exact declared
edges. This narrowly scoped internal split preserves composition ownership and
the CLI root boundary; other builders retain the existing direction rules.

Desde #158, a interface CLI pode importar funções nomeadas de
`composition.cli`; essa é a única fronteira em que a travessia transitiva da
apresentação para a infraestrutura termina. Os módulos de composição anteriores
continuam implementando a montagem por capacidade, chamados por essa fronteira.
A regra reutiliza `OWNERS`, `ALLOWED_INTERNAL_EDGES`, o resolvedor de imports e
as regras externas de #133. Não há uma segunda lista de permissões arquiteturais.
Reexports e imports transitivos de adaptadores concretos, SQLite, HTTP ou
bibliotecas EPUB/HTML são rejeitados mesmo quando uma aresta intermediária tem
exceção. Typer/Rich continuam válidos na apresentação e proibidos em aplicação/domínio.

A apresentação não pode importar classes reexportadas pela composição nem
esconder construção através de um alias de módulo. A composição não pode manter
instâncias/mapeamentos mutáveis globais, usar `global`/`nonlocal`, decorar
factories para cachear instâncias ou construir recursos em argumentos padrão.
Testes de execução instrumentam também importação limpa, ajuda, transporte
HTTP/DNS/socket, sessões de provedor e repositórios para verificar construção
tardia e escopo. Isso complementa a análise estática, não cria um sandbox.

External package roots are explicitly recognized without importing or requiring
their installation. CLI presenters can import Typer/Rich; Desktop presenters
can import recognized Qt bindings. Formats can import ebooklib/Beautiful
Soup/lxml; providers can import requests. Domain/application cannot import any
recognized vendor library. Concrete standard-library SQL, subprocess, HTTP,
XML, archive, CSV, filesystem, socket and persistent-store libraries are also
rejected in inward layers and interfaces. Standard-library presentation
libraries (including tkinter/curses) are restricted to interfaces/composition.
Neutral values such as `pathlib.Path` remain compatible with the
existing ADR baseline. Current mixed modules use exact exceptions instead of a
permissive legacy layer.

## Temporary exceptions

`EXCEPTIONS` is the authoritative reviewed list. Every entry identifies an exact
source/destination module edge, capability owner, positive removal issue number,
and observable expiry condition. Wildcards, incomplete entries, and exceptions
that no longer waive a forbidden edge fail. Exceptions cannot waive unknown
imports, syntax/read errors, cycles or missing ownership.

As exceções de apresentação CLI para cache, projeto, configuração, formato,
revisão, biblioteca, lote, glossário, provedor e composições por capacidade foram
removidas em #158. A matriz compartilhada de #123 registra os serviços entregues
pelas dependências, o inventário de helpers e a única fronteira de construção.
`preflight` continua sendo composição: conecta os adaptadores entregues de
formato, cache, glossário e provedor. Sua aresta para `composition.builders.providers`
permanece uma exceção exata de composição entre capacidades, sem caminho direto
da apresentação até esse módulo. A numeração #158 nessa exceção identifica a
consolidação responsável, não uma autorização para duplicar serviços.
The coupling of the
`v1.html_translate` compatibility facade expires in #153, since #145 keeps the v1 cache.
EPUB pipeline coupling expires in #144, after #141 separates the
provider contract from its HTTP adapter. `formats.epub.html_structure` has no such coupling.
Legacy review projection coupling expires in #158. Fuzzy memory's exact-cache and vendor coupling
expires in #161. The guided library flow uses `library.service` and `library.adapters`.
The exact edge and condition for each of these are recorded individually in
the test data; these references do not imply that those issues are completed.

To request an exception, explain the existing mixed responsibility, add only
the exact edge, name the capability owner and removal issue, specify when the
edge disappears, and obtain normal human change review. A new architecture
choice needs its own decision review. When an extraction removes an edge,
remove its exception in the same change. Never exempt an entire layer, package
prefix or future family of modules.

## Evidence and limits

The analyzer parses every production Python file with `ast`; it does not execute
fixtures, production modules, Qt, or network clients. Absolute, relative,
multilevel and aliased imports resolve to exact module edges and are tested
directly. Imports inside functions and `TYPE_CHECKING` blocks are checked too. Star imports, unknown
internal modules/vendor roots, unreadable files/directories, invalid syntax,
symlinks and ambiguous module/package names fail closed.

[Negative fixtures](../../tests/fixtures/architecture_boundaries/negative.json)
contain synthetic source snippets and expected rejection categories. They are
written to temporary complete package trees and scanned by the same analyzer
as `src/ayvu`. Further tests cover alias equivalence, relative resolution,
package reexports, missing ownership, cycles, read failures and exception expiry.
The normal suite needs only the existing dev dependencies, with no Desktop
extra or new runtime framework.

Static imports cannot prove that an adapter respects product permissions,
consent, retention, or fallback policy; that composition contains only
construction; or that values never carry concrete framework objects. They also
cannot detect computed imports, reflective loading, runtime plugin discovery,
or all attribute access hidden behind a module alias. An allowed module edge
also cannot prove that every imported symbol is a port rather than a concrete
implementation; public API review and contract tests remain required. Such
behavior still requires code review and the relevant use-case, policy and
security tests.
No passing import check constitutes a sandbox or proves full ADR compliance.
