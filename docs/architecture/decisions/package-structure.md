# Estrutura de pacotes e regras de dependência

Status: Proposed

Decision-Date: `2026-11-10`

Owners: owner/mantenedor do repositório Ayvu (aceite); a refatoração é executada pelo Claude Code,
por decisão do owner em 2026-11-10.

Issue: [#267 — REF-02](https://github.com/MichaelAlexandreDev/ayvu/issues/267).

Base: `main` em `7902aba`. A medição de partida está em [refactor-baseline.md](../refactor-baseline.md),
reproduzível com `scripts/arch_metrics.py`.

## Contexto

As [fronteiras do monólito modular](../modular-monolith-boundaries.md) já são `Accepted`. Elas nomeiam
módulos (Projects, Jobs, Content, Translation, Knowledge, Formats, Providers, Review, Provenance,
Artifacts, Policies) e camadas (Domain, Application, adaptadores, raízes de composição). As pastas, porém,
não refletem isso:
- `src/ayvu` tem 116 arquivos e 37.935 linhas, quase todos no mesmo nível;
- 30 arquivos passam de 400 linhas e 8 de 800; 18 funções passam de 80 linhas;
- 12 pares de grupos de módulos se importam nos dois sentidos.

Depois de estudar as alternativas, o owner indicou esta composição:
- monólito modular por fora, hexagonal na medida dentro de cada módulo;
- pipeline no centro da tradução;
- plugins internos para tradutores, formatos e papéis de IA;
- cliente-servidor só para o Lite servidor e o celular;
- edições escolhidas na composição;
- DDD estratégico;
- migração por strangler fig.

Respostas do owner em 2026-11-10:
- o núcleo compartilhado se chama `kernel/`;
- arquivos têm até 400 linhas, testes incluídos, e funções até 80;
- `import-linter` verifica as fronteiras;
- sem shims de compatibilidade;
- a refatoração vem primeiro, sem issues novas de funcionalidade até ela terminar;
- o runtime v1 deve ser aposentado, não mantido;
- a ordem de camadas (D3) foi aceita como direção.

## Decisões

### D1. Monólito modular, hexagonal na medida
- Pastas por capacidade. Dentro de cada módulo, começar plano: `model.py`, `service.py`, `store.py`.
- Um módulo só ganha subpacotes (`domain/`, `application/`, `adapters/`) quando passa de uns 6 arquivos
  ou de 2.000 linhas.
- Interface (porta) só onde existe, ou vai existir de verdade, mais de uma implementação.

### D2. Uma API pública por módulo
- O `__init__.py` de cada módulo lista o que é público.
- Outros módulos importam só essa API: `ayvu.knowledge.cache`, nunca `ayvu.knowledge.cache.admin_v2_store`.
- Dentro do próprio módulo, os imports internos são livres.

### D3. Camadas com direção única
Quem está embaixo nunca importa quem está em cima. Módulos na mesma linha não se importam entre si,
salvo onde a linha diz o contrário.

| Camada (de cima para baixo) | Módulos |
| --- | --- |
| 1 | `interfaces` (cli, desktop, server) |
| 2 | `composition` |
| 3 | `v1` |
| 4 | `recovery` |
| 5 | `translation`, `review` |
| 6 | `projects` |
| 7 | `knowledge`, `providers`, `formats` |
| 8 | `artifacts` |
| 9 | `jobs` |
| 10 | `content`, `provenance`, `network`, `preferences`, `library`, `application` |
| 11 | `kernel` |

`application/` guarda só os contratos realmente transversais que sobrarem de `cli_contracts.py`.

### D4. `kernel/` pequeno de propósito
- Recebe identificadores, validação de UTC, cancelamento, falhas e avisos tipados, o executor de
  migrações SQLite e os valores de domínio comuns (idiomas).
- Só entra o que três ou mais módulos usam. O `kernel` não importa nenhum outro módulo do Ayvu.

### D5. A composição sai dos módulos
- Os `*_composition.py` vão para `composition/builders/`. As raízes ficam em `composition/cli.py`,
  `composition/desktop.py` e, no futuro, `composition/server.py`.
- Uma raiz escolhe e constrói adaptadores. Não executa etapas de tradução nem acumula regra de negócio.

### D6. Edições na composição
- `composition/editions.py` declara o que cada edição liga (Lite, Free, AI).
- Nenhum módulo testa a edição. Plugins pagos podem morar em pacotes fechados separados.
- A definição das edições depende do owner e da #100 (REF-17).

### D7. Runtime v1 em quarentena
- O v1 é o padrão de hoje quando não há rótulo de modelo. Os arquivos do v1 vão para `v1/`.
- `v1` pode importar qualquer módulo; nenhum módulo importa `v1`, só `composition`.
- O owner decidiu aposentar o v1. Como e quando é a REF-18, depois de listar o que só o v1 faz.

### D8. Sem shims
- Mover um arquivo atualiza todos os imports na mesma PR. Nenhum arquivo fica só reexportando o novo lugar.
- Os entrypoints (`ayvu`, `ayvu-desktop`) mudam de caminho no `pyproject.toml`; os comandos continuam iguais.
- Justificativa: o repositório é privado, não está no PyPI e ninguém de fora importa o Ayvu.

### D9. Identidades persistidas congeladas
- `tests/test_persisted_identities.py` (REF-01) congela as tags de domínio, as versões e os digests.
- Nenhuma classe que entra em fingerprint é renomeada (`CreateCandidate`, `DeriveRevision`, `ReviewRevision`).
- Mudar uma identidade exige migração, novo número de versão e uma decisão explícita na PR.

### D10. Responsabilidade única com catraca de tamanho
- Arquivo novo: até 400 linhas, testes incluídos. Função nova: até 80 linhas.
- Os arquivos e funções maiores de hoje ficam numa lista com o tamanho atual e só podem diminuir.
- O tamanho é o alarme. O critério é "este arquivo tem um motivo só para mudar?".

### D11. Fronteiras checadas pelo `import-linter`
- Dependência só de desenvolvimento. Um contrato de camadas espelha a D3, e contratos de independência
  separam os módulos da mesma camada.
- As violações de hoje ficam numa lista de exceções que só diminui.
- O `test_architecture_boundaries.py` é absorvido aos poucos.

### D12. Testes espelhando os pacotes
- `tests/<modulo>/test_*.py`; fakes e fixtures compartilhados em `tests/support/`.
- O `test_cli.py` é dividido junto com o `cli.py`, e o mapa `CORE_TEST_FILES` acompanha.

### D13. Processo
- Uma migração aberta por vez.
- Cada PR é só mecânica (mover e renomear imports) ou só de comportamento, nunca as duas.
- `git mv` mantém o histórico.
- Toda PR prova que nada mudou:
  - a suíte nos dois modos e a etapa Docker;
  - as comparações com o `main` (71 cenários de CLI, 42 de tradução e retomada, e2e com servidor HTTP real);
  - o teste de identidades;
  - `scripts/arch_metrics.py`, com os números descendo.
- Nenhuma issue nova de funcionalidade até a refatoração terminar.

### D14. Manter a arquitetura com o tempo
- **Checagens no CI:** catraca (D10), contratos de importação (D11) e checagem de API pública (D2).
- **Guia "onde fica o código novo"** em `docs/architecture/` e no AGENTS.md, lido por pessoas e por agentes de IA.
- **Checklist de PR** para o que ferramenta não pega: regra no módulo certo, duplicação, divisão só para passar no limite.
- **Mudar uma regra é permitido, mas visível:** muda esta decisão e o contrato na mesma PR revisada.
- **`scripts/arch_metrics.py` no checklist de release**, para acompanhar a tendência.

## Árvore alvo

```text
src/ayvu/
  kernel/          ids, clock, cancellation, failures, values; sqlite/ (migrations, contracts)
  network/         policy, http
  preferences/     model, service, store, config_facade
  library/         service, adapters
  provenance/      model, service, store
  content/         model, use_cases, protection
  application/     contracts (só o transversal)
  jobs/            model, events, execution, service, store
  artifacts/       model, service, store, destination
  formats/         contracts, service, local; epub/ (archive, adapter, io, html_structure, validation); markdown/
  knowledge/       cache/ (identity, schema, repository, interchange, admin, admin_store, admin_v2_store);
                   memory/ (fuzzy; approved/ model, service, store); glossary/ (rules, catalog, catalog_store)
  providers/       capabilities, discovery, service, network; libretranslate/ (adapter)
  projects/        model, service, store, schema; profiles/ (model, service, store, glossary_store)
  translation/     ports, plan, pipeline, engine, document, chunking, preflight, project, batch, batch_output;
                   stages/ (memory_retrieval); context/ (model, execution, source)
  review/          application, exchange, csv_export, csv_import
  recovery/        service, store
  v1/              translation, html_translate, translator, cache, cache_format, cache_stream,
                   cache_admin_store, resume, resume_scan
  composition/     cli, desktop, editions (futuro); builders/
  interfaces/      cli/ (app, commands/, render, progress); desktop/; server/ (futuro)
```

## Mapa arquivo por arquivo

Cada arquivo de produção em `7902aba` aparece uma vez. "dividir" marca os arquivos acima de 800 linhas,
que a REF-14 e a REF-15 quebram por motivo de mudança, depois de movidos.

| Atual | Destino | Nota |
| --- | --- | --- |
| `__init__.py` | `__init__.py` |  |
| `domain.py` | `kernel/values.py` | divide por dono quando surgir um |
| `migration_contracts.py` | `kernel/sqlite/contracts.py` |  |
| `sqlite_migrations.py` | `kernel/sqlite/migrations.py` |  |
| `network_policy.py` | `network/policy.py` |  |
| `network_http.py` | `network/http.py` |  |
| `preferences.py` | `preferences/model.py` | junta com `config_values.py` |
| `config_values.py` | `preferences/model.py` | junta com `preferences.py` |
| `preferences_service.py` | `preferences/service.py` |  |
| `preferences_store.py` | `preferences/store.py` |  |
| `config.py` | `preferences/config_facade.py` |  |
| `library_service.py` | `library/service.py` |  |
| `library_adapters.py` | `library/adapters.py` |  |
| `provenance.py` | `provenance/model.py` |  |
| `provenance_service.py` | `provenance/service.py` |  |
| `provenance_store.py` | `provenance/store.py` |  |
| `content.py` | `content/model.py` |  |
| `content_use_cases.py` | `content/use_cases.py` |  |
| `translation_protection.py` | `content/protection.py` | usado por formats e translation |
| `job.py` | `jobs/model.py` | `is_slug`, `validate_job_identity` → `kernel/ids.py`; `validate_utc` → `kernel/clock.py` |
| `job_events.py` | `jobs/events.py` | `JobFailure`, `JobFailureFamily`, `JobWarning`, `normalize_failure` → `kernel/failures.py` |
| `job_execution.py` | `jobs/execution.py` | `CancellationToken`, `CancellationStatus`, `JobCancellationError` → `kernel/cancellation.py` |
| `job_service.py` | `jobs/service.py` |  |
| `job_store.py` | `jobs/store.py` |  |
| `artifact.py` | `artifacts/model.py` |  |
| `artifact_service.py` | `artifacts/service.py` |  |
| `artifact_store.py` | `artifacts/store.py` |  |
| `artifact_destination.py` | `artifacts/destination.py` | passa a chamar formats por uma porta |
| `format_adapter.py` | `formats/contracts.py` |  |
| `format_service.py` | `formats/service.py` |  |
| `format_local.py` | `formats/local.py` |  |
| `epub_archive.py` | `formats/epub/archive.py` |  |
| `epub_format.py` | `formats/epub/adapter.py` |  |
| `epub_io.py` | `formats/epub/io.py` | dividir; o CSV de revisão sai para review ou v1 |
| `html_structure.py` | `formats/epub/html_structure.py` |  |
| `validation.py` | `formats/epub/validation.py` |  |
| `markdown_writer.py` | `formats/markdown/writer.py` |  |
| `cache_identity.py` | `knowledge/cache/identity.py` | dividir |
| `cache_schema.py` | `knowledge/cache/schema.py` |  |
| `cache_repository.py` | `knowledge/cache/repository.py` |  |
| `cache_interchange.py` | `knowledge/cache/interchange.py` |  |
| `cache_admin.py` | `knowledge/cache/admin.py` |  |
| `cache_admin_store.py` | `knowledge/cache/admin_store.py` |  |
| `cache_admin_v2_store.py` | `knowledge/cache/admin_v2_store.py` | dividir |
| `translation_memory.py` | `knowledge/memory/fuzzy.py` |  |
| `approved_memory.py` | `knowledge/memory/approved/model.py` |  |
| `approved_memory_service.py` | `knowledge/memory/approved/service.py` |  |
| `approved_memory_store.py` | `knowledge/memory/approved/store.py` |  |
| `glossary.py` | `knowledge/glossary/rules.py` |  |
| `glossary_catalog.py` | `knowledge/glossary/catalog.py` |  |
| `glossary_catalog_store.py` | `knowledge/glossary/catalog_store.py` |  |
| `provider_capabilities.py` | `providers/capabilities.py` |  |
| `provider_discovery.py` | `providers/discovery.py` |  |
| `provider_service.py` | `providers/service.py` |  |
| `provider_network.py` | `providers/network.py` |  |
| `provider_libretranslate.py` | `providers/libretranslate/adapter.py` |  |
| `project.py` | `projects/model.py` |  |
| `project_service.py` | `projects/service.py` |  |
| `project_store.py` | `projects/store.py` |  |
| `project_schema.py` | `projects/schema.py` |  |
| `project_profiles.py` | `projects/profiles/model.py` |  |
| `project_profile_service.py` | `projects/profiles/service.py` |  |
| `project_profile_store.py` | `projects/profiles/store.py` |  |
| `profile_glossary_store.py` | `projects/profiles/glossary_store.py` |  |
| `provider.py` | `translation/ports.py` | a porta do provedor pertence à tradução |
| `translation_plan.py` | `translation/plan.py` |  |
| `translation_pipeline.py` | `translation/pipeline.py` |  |
| `translation_engine.py` | `translation/engine.py` |  |
| `translation_document.py` | `translation/document.py` | dividir |
| `chunking.py` | `translation/chunking.py` |  |
| `preflight.py` | `translation/preflight.py` |  |
| `translate_project.py` | `translation/project.py` | dividir |
| `translate_batch.py` | `translation/batch.py` |  |
| `batch_output_policy.py` | `translation/batch_output.py` |  |
| `memory_retrieval.py` | `translation/stages/memory_retrieval.py` | etapa do pipeline, não knowledge |
| `translation_context.py` | `translation/context/model.py` |  |
| `context_execution.py` | `translation/context/execution.py` |  |
| `context_source.py` | `translation/context/source.py` |  |
| `review_application.py` | `review/application.py` |  |
| `review_exchange.py` | `review/exchange.py` | dividir |
| `review_export.py` | `review/csv_export.py` |  |
| `review_import.py` | `review/csv_import.py` |  |
| `job_recovery.py` | `recovery/service.py` | orquestração de retomada, sai de jobs |
| `job_recovery_store.py` | `recovery/store.py` |  |
| `legacy_translation.py` | `v1/translation.py` |  |
| `html_translate.py` | `v1/html_translate.py` |  |
| `translator.py` | `v1/translator.py` |  |
| `cache.py` | `v1/cache.py` |  |
| `cache_legacy_format.py` | `v1/cache_format.py` |  |
| `cache_legacy_stream.py` | `v1/cache_stream.py` |  |
| `cache_admin_legacy_store.py` | `v1/cache_admin_store.py` |  |
| `resume.py` | `v1/resume.py` |  |
| `legacy_resume.py` | `v1/resume_scan.py` |  |
| `cli_contracts.py` | `application/contracts.py` | cada erro ou contrato vai ao módulo dono; fica só o transversal |
| `cli_composition.py` | `composition/cli.py` |  |
| `desktop/bootstrap/composition.py` | `composition/desktop.py` |  |
| `cache_admin_composition.py` | `composition/builders/cache_admin.py` |  |
| `content_composition.py` | `composition/builders/content.py` |  |
| `glossary_catalog_composition.py` | `composition/builders/glossary.py` |  |
| `project_profile_composition.py` | `composition/builders/profiles.py` |  |
| `provider_composition.py` | `composition/builders/providers.py` |  |
| `review_composition.py` | `composition/builders/review.py` | dividir; tem casos de uso dentro |
| `translate_batch_composition.py` | `composition/builders/translate_batch.py` |  |
| `translate_project_composition.py` | `composition/builders/translate_project.py` |  |
| `cli.py` | `interfaces/cli/app.py` | dividir em `interfaces/cli/commands/` e `render.py` (REF-14) |
| `cli_progress.py` | `interfaces/cli/progress.py` |  |
| `desktop_bootstrap.py` | `interfaces/desktop/bootstrap.py` | entrypoint `ayvu-desktop` |
| `desktop/__init__.py` | `interfaces/desktop/__init__.py` |  |
| `desktop/adapters/__init__.py` | `interfaces/desktop/adapters/__init__.py` |  |
| `desktop/adapters/qt/__init__.py` | `interfaces/desktop/adapters/qt/__init__.py` |  |
| `desktop/bootstrap/__init__.py` | — | removido: o pacote só guardava a raiz, que vai para `composition/desktop.py` |
| `desktop/platform/__init__.py` | `interfaces/desktop/platform/__init__.py` |  |
| `desktop/presentation/__init__.py` | `interfaces/desktop/presentation/__init__.py` |  |
| `desktop/presentation/views/__init__.py` | `interfaces/desktop/presentation/views/__init__.py` |  |
| `desktop/resources/__init__.py` | `interfaces/desktop/resources/__init__.py` |  |
| `desktop/viewmodels/__init__.py` | `interfaces/desktop/viewmodels/__init__.py` |  |

## Ciclos e quem os desfaz

| Par | Causa | Desfeito por |
| --- | --- | --- |
| jobs ↔ projects | `job_recovery_store` usa projetos; `project_schema` usa jobs | REF-08 (`recovery/`) |
| jobs ↔ provenance | `job_recovery_store` usa proveniência; proveniência usa identidades de job | REF-04 (kernel) e REF-08 |
| artifacts ↔ jobs | `job_recovery_store` usa artefatos; artefatos usam identidades e cancelamento de job | REF-04 e REF-08 |
| jobs ↔ translation | `job_recovery` chama `translate_project` | REF-08 |
| interfaces ↔ jobs | `resume` usa `cli_contracts` | REF-04 (divisão do `cli_contracts`) |
| interfaces ↔ providers | `provider_composition` usa `cli_contracts` | REF-04 e REF-05 |
| interfaces ↔ translation | `preflight`, `batch_output_policy` e `translate_project_composition` usam `cli_contracts` | REF-04 e REF-05 |
| content ↔ formats | `content_composition` monta formatos | REF-05 |
| formats ↔ review | `epub_io` lê e escreve o CSV de revisão | REF-10 |
| formats ↔ translation | `html_structure` e `html_translate` usam `translation_protection` e o pipeline | REF-06 (`html_translate` para v1) e REF-07 (`content/protection`) |
| knowledge ↔ translation | `memory_retrieval` (em knowledge) usa `translation_plan` e `translation_protection` | REF-07 e REF-09 (`memory_retrieval` para o pipeline) |
| artifacts ↔ formats | `artifact_destination` chama `format_local` e `format_service` | REF-08 (porta) |

## Sequência

| Issue | O que faz | Tamanho |
| --- | --- | --- |
| REF-03 | Catraca de tamanho, `import-linter` com as exceções de hoje, checagem de API pública | M |
| REF-04 | Extrair o `kernel` e dividir o `cli_contracts` | M |
| REF-05 | Tirar a composição de dentro dos módulos | M |
| REF-06 | Quarentena do v1 | M |
| REF-07 | Mover os módulos folha: network, preferences, library, provenance, content | M |
| REF-08 | Mover jobs, artifacts e projects; criar `recovery/`; porta artifacts → formats | L |
| REF-09 | Mover knowledge; `memory_retrieval` para o pipeline | M |
| REF-10 | Mover formats; tirar a revisão do `epub_io` | M |
| REF-11 | Mover providers e translation | M |
| REF-12 | Mover review | S |
| REF-13 | Interfaces: desktop e esqueleto do CLI; entrypoints | S |
| REF-14 | Dividir o `cli.py` por grupo de comandos (cerca de 10 PRs) | L |
| REF-15 | Dividir os módulos acima de 800 linhas (uma issue por família) | L |
| REF-16 | Testes espelhando os pacotes; dividir o `test_cli.py` | M |
| REF-17 | Raízes de composição por edição | M |
| REF-18 | Inventário e aposentadoria do v1 | M |
| REF-19 | Guia "onde fica o código novo", seção no AGENTS.md e checklist de PR | S |

## Alternativas rejeitadas

| Alternativa | Por que não |
| --- | --- |
| Só camadas técnicas (`domain/`, `services/`, `adapters/` globais) | Espalha cada capacidade por três pastas e não desfaz os ciclos entre capacidades |
| Hexagonal completo em todo módulo | Burocracia para módulos simples, como preferências e biblioteca |
| Vertical slices por funcionalidade | O Ayvu tem muita regra compartilhada (cache, glossário, pipeline); fatias duplicariam |
| Microsserviços | Rede, deploy e consistência sem benefício para um app local de um mantenedor |
| Shims de compatibilidade | Ninguém de fora importa o Ayvu; dobrariam os caminhos |
| Reescrever do zero | Perde as correções acumuladas e fica meses sem entregar |

## Questões em aberto

1. A lista de funções de cada edição (REF-17) depende do owner.
2. Como aposentar o v1 e o que fazer com os caches e retomadas antigos depende do inventário da REF-18.
3. Se `application/` ainda precisar existir depois da REF-04, ou se tudo cabe nos módulos donos.

## Gate de aceite

O owner aceita, restringe ou rejeita cada decisão (D1 a D14), a árvore e a sequência, numa mudança
revisada separada que troca `Status: Proposed` por `Status: Accepted` e registra a data e o que foi
restringido. Merge, fechamento de issue ou publicação enquanto o registro está `Proposed` não satisfazem
o gate. Nenhum arquivo de produção é movido antes do aceite.

Checklist:
- [ ] D1 a D14 aceitas, com restrições anotadas
- [ ] árvore alvo e mapa arquivo por arquivo aceitos
- [ ] sequência REF-03 a REF-19 aceita
