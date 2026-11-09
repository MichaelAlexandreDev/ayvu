# Memória de tradução aprovada e governada

Implementa [#161 / DESKTOP-130](https://github.com/DevStarrk1137/ayvu/issues/161).
Os artefatos de #135, #138, #140 e #154 são utilizados sem alterar seus contratos
ou migrações publicadas. A integração de recuperação no pipeline e na CLI é descrita abaixo (#162).

## Contratos e armazenamento

`approved_memory.py` define valores e regras de domínio;
`approved_memory_service.py` define o port `ApprovedMemoryRepository`, comandos
e os casos de uso `ApprovedMemory`. `approved_memory_store.py` implementa o
adaptador de produção `SqliteApprovedMemoryRepository` e seu bootstrap explícito.
SQL e migrações permanecem na infraestrutura.

A composição usa o diretório do projeto já validado por
`ProjectJsonRepository.resolve_database_paths(project_id).database.parent`:

```python
from ayvu.approved_memory_service import ApprovedMemory
from ayvu.approved_memory_store import open_approved_memory

repository = open_approved_memory(
    paths.database.parent,
    allowed_scopes=frozenset(authorized_scopes),
    provenance={project_id: provenance_repository},
)
memory = ApprovedMemory(repository)
```

`authorized_scopes` vem da composição confiável, nunca do texto do documento.
Cada repositório de proveniência fornecido implementa o port de #138 e resolve
referências daquele projeto. Leituras entre projetos exigem referências de origem
e escopos autorizados explícitos; não existe descoberta automática de outros bancos.

O layout adicional é:

- `projects/<UUID>/approved-memory.sqlite`: dados linguísticos sensíveis;
- `projects/<UUID>/backups/approved-memory-schema/`: snapshots de upgrade;
- `.approved-memory.sqlite.migrate.lock`: lock do migrador no mesmo diretório.

O registro `MEMORY_MIGRATIONS` é independente de `PROJECT_MIGRATIONS` e do cache:
versão 1 contém apenas infraestrutura; versão 2 cria `tm_revisions`, `tm_events`,
`tm_approvals` e `tm_operations`; a versão 3 acrescenta apenas índices de recuperação por
escopo/idiomas/fonte e chave linguística, preservando os checksums publicados. O runner de #135 controla criação, verificação,
permissões, backups, limites, lock e upgrades. O banco de projeto continua na
versão 4. A tradução rich/v2 abre a memória somente com `--translation-memory` explícito.

Não há leitura, cópia, promoção ou migração de `translations` ou `cache_entries_v2`.
Limpeza, expiração e substituição do cache não alcançam este arquivo. Depreciação
de memória não toca no cache. Sua retenção é explícita: fatos históricos não são
expurgados automaticamente e o port não fornece exclusão de revisões. Uma remoção
administrativa do banco inteiro, com seus acessos parados, é independente do cache.
Snapshots contêm dados sensíveis de TM e devem ser preservados privadamente junto
das referências de proveniência do projeto. Não são exportações de cache ou de
treinamento. Não há exportação de dataset, sinalizador de consentimento ou
elegibilidade para compartilhamento inferida de aprovação.

## Identidade e valores

Cada revisão guarda separadamente projeto de origem, documento e revisão
estruturais, `unit_id` e `source_digest` de #140, texto fonte canônico visível,
chave de busca, versões de normalização/busca, idiomas, alvo, kind, stage,
revisões de pipeline/perfil/glossário, escopo e referências de proveniência.
Não guarda documentos inteiros, caminhos, locators brutos ou blobs de proveniência.

`MemorySource.from_document(document, unit_id)` seleciona uma unidade real.
Unidades com spans `markup` exigem `visible_text=` fornecido pela projeção do
adaptador de formato: remover tags no domínio perderia quebras de linha de `br`
ou poderia incluir conteúdo oculto. Literais, identificadores e placeholders
visíveis permanecem na projeção. Unidades sem markup usam seu texto original e
recusam uma projeção divergente. A memória normaliza a fonte visível em NFC;
a chave usa NFC, casefold e colapso de whitespace. Ambas as políticas guardam a
versão do algoritmo e do Unicode. A identidade exata de conteúdo permanece a do
modelo, mesmo quando duas formas Unicode produzem a mesma chave linguística.
Texto e chaves não são identidade estrutural nem evidência de aprovação.

Fonte linguística e alvo aceitam no máximo 65.536 bytes UTF-8 cada; excesso é
recusado, sem truncamento, inclusive antes da normalização da fonte visível.
`repr`, logs e erros não exibem valores, chaves, hashes de conteúdo, credenciais
ou caminhos. Resultados de leitura carregam os valores para o chamador autorizado.

| Kind | Stage | Pode receber aprovação humana? |
| --- | --- | --- |
| `provider_response` | `provider` | Não |
| `restored_visible` | `protected_restored` | Não |
| `glossary_derived` | `terminology_applied` | Não |
| `linguistic_final` | `linguistic_final` | Sim |

`linguistic_final` é o texto visível após restauração de tokens protegidos e todas
as transformações linguísticas determinísticas selecionadas, incluindo glossário
efetivo aplicado após o cache, antes da reconstrução de formato. A composição
deve fornecer este valor e evidência correspondente; o repositório não executa o
pipeline nem converte um valor anterior em final. `PipelinePin` exige referências
imutáveis `pipeline`, `profile` e `glossary`, inclusive uma revisão que represente
explicitamente ausência de glossário. Igualdade de strings com o agregado
operacional de #154 não une essas identidades.

## Histórico e revisão explícita

A tabela fechada `TRANSITIONS` autoriza exclusivamente:

| Estado | Próximos estados |
| --- | --- |
| `machine_candidate` | `human_edited`, `approved`, `rejected`, `deprecated` |
| `human_edited` | `approved`, `rejected`, `deprecated` |
| `approved` | `deprecated`, `superseded` |
| `rejected`, `deprecated`, `superseded` | Nenhum |

`create_candidate(CreateCandidate(...))` cria uma entrada e sua primeira revisão.
`record_human_edit(...)` cria outra revisão da mesma entrada: registra a criação
e a transição humana na mesma transação, sem reescrever a revisão anterior.
`create_revision(DeriveRevision(...))` permite nova tentativa ou derivação de
qualquer revisão histórica, sempre sem aprovação herdada. Isso permite editar
um aprovado ou recomeçar após estado terminal sem transição ilegal no pai.
O vínculo `parent_revision`, o número sequencial por entrada e os eventos
preservam as ramificações concorrentes. Mudança de fonte/idiomas exige nova entrada;
mudança de alvo, efeito de glossário, escopo ou kind/stage exige nova revisão.

Antes de cada comando, o chamador registra um evento de #138. A referência deve
existir, estar atual, pertencer ao projeto/unidade, trazer `operation_id` do
comando, as três referências de configuração e os pais exigidos. Operações são
`tm_create`, `tm_derive`, `tm_edit`, `tm_approve`, `tm_reject` e `tm_deprecate`.
Edição e revisão exigem `ActorKind.HUMAN`; preserva-se a decisão de privacidade
de #138 de registrar a espécie de ator, sem identificar nominalmente a pessoa.
Uma origem importada exige referência adicional a `tm_import`, ação humana
explícita, incluída nos pais de cada novo evento da cadeia importada.

`approve(...)` exige o `TargetValue` exato revisado, o escopo, a versão atual de
eventos da revisão e `expected_approval` (ID da aprovação corrente ou `None`).
Inclusive `machine_candidate -> approved` exige essa ação humana, data e
proveniência. Scores, armazenamento, reuso e políticas não aprovam entradas.
`reject(...)` e `deprecate(...)` também exigem revisão explícita e versão esperada.
`superseded` só pode ser produzido pela substituição transacional de aprovação.

Valores, eventos e operações possuem triggers contra UPDATE/DELETE. A única
projeção mutável é o índice de aprovações atuais, único por escopo, origem
estrutural/conteúdo, idiomas e stage. `BEGIN IMMEDIATE` verifica versões e troca
esse índice junto com os eventos de aprovação/supersessão. Dois concorrentes
divergentes com a mesma aprovação esperada produzem um vencedor e um
`MemoryConflictError`, mantendo candidatos e suas proveniências. Se falhar após
a supersessão, toda a transação de TM é revertida. Contenção que excede o limite
de espera do escritor também retorna conflito tipado (`concurrent_write`), sem
registrar uma operação parcial; o chamador pode repetir o comando exato.

O evento externo de #138 é registrado antes da transação de TM: uma recusa ou
queda pode deixar um fato de tentativa sem aplicação em TM. Não há aprovação
parcial nem duplicação do payload de #138 entre bancos. O ID da operação e o hash
privado do comando permitem replay exato com o snapshot original; reutilização
do ID com outros parâmetros é conflito, inclusive após transições posteriores.

## Escopos, consultas e erros

`MemoryScope` exige tipo fechado (`project`, `work`, `user`, `import`) e UUID do
proprietário. Escopo de projeto deve coincidir com o projeto fonte; obra, usuário
e coleção importada são concessões explícitas, nunca inferidas da localização
do cache ou de texto igual. A composição pode autorizar vários proprietários.
Toda query exige tupla explícita de escopos contidos nessa autorização; acesso
direto fora dela é `MemoryScopeError`. `current(...)` respeita a precedência na
ordem exata dessa tupla e compara somente identidades estruturais/conteúdo e par
de idiomas, sem fallback por chave ou texto.

`get`, `current`, `history` e `list_entries` existem no serviço e no port.
Listagem usa cursor de sequência crescente, até 256 revisões por página, filtro
opcional de estado/entrada e cursor seguinte explícito. Histórico usa versão de
evento como cursor, também com limite. Consultas de estado são resolvidas por SQL,
sem carregar a memória inteira. Estados terminais permanecem auditáveis mas nunca
são retornados por `current`. Erros tipados distinguem entrada ausente, conflito,
transição inválida, escopo negado, kind/stage incompatível, proveniência inválida,
validação e falha de armazenamento; mensagens contêm apenas códigos fixos.

## Recuperação conservadora (#162 / DESKTOP-131)

`MemoryQuery`, `RetrievalMode` e `RetrievalLimits` são contratos de domínio.
`ApprovedMemoryRepository.retrieve` é a única fronteira de consultas do pipeline;
`MemoryRetrieval` decide elegibilidade, similaridade, ranking e aplicação. O
`DocumentTranslator` é o pipeline de segmentos de produção disponível nesta main
(o nome histórico `SegmentTranslationPipeline` da issue não é uma classe existente).
`RichTranslationRuntime` compõe os adaptadores reais; não há leitor SQL no pipeline
nem consumidor Desktop alegado.

A ordem fixa é cache exato v2 com identidade completa de #153/#154, memória aprovada
exata, fuzzy aprovado, compatibilidade de cache opt-in, provedor. Cache-only permanece
exclusivamente exato. `RetrievalPolicy` é compartilhada entre a aplicação e a CLI;
os seletores da interface não calculam scores ou confiança.

Só ponteiros atuais de `tm_approvals`, com evento mais recente `approved`, idiomas,
escopo autorizado, políticas de busca/normalização fixadas e
`linguistic_final`/`linguistic_final` entram na recuperação aprovada. Exato significa
igualdade NFC da fonte canônica inteira, incluindo espaços, e não igualdade de
`unit_id`, digest de conteúdo ou chave casefold. Fuzzy usa a razão Indel Unicode
determinística existente, sobre NFC/casefold/whitespace versionados. Concordância
busca substring dessa chave no conjunto limitado e nunca aplica automaticamente.

O adaptador usa índices por escopo/idiomas e fonte/chave de busca. A busca aproximada
examina **no máximo 64 linhas atuais por faixa e pin**, na faixa do primeiro ponto
Unicode da chave, ordenadas por chave/sequence; ordena esse conjunto por distância
de comprimento, número da revisão decrescente e ID estável antes de materializar
valores. Isso limita o recall: não é busca exaustiva de substring ou fuzzy em toda
a memória. A aplicação ordena hits automáticos primeiro, escopos na precedência
explícita do chamador, score decrescente, revisão decrescente e IDs estáveis. Pins
compatíveis são consultados antes dos incompatíveis. O runtime autoriza apenas o
escopo do projeto atual; a API aceita outros escopos explicitamente autorizados.

Cada consulta aceita 20 candidatos/256 KiB por padrão, no máximo 64/1 MiB, até
64 KiB UTF-8 de fonte e alvo individualmente. O chamador só reduz esses tetos.
`N+1` inválido falha na construção da consulta, antes do port. O SQL verifica
comprimentos armazenados antes de `_load`; a aplicação também recusa ports que
excedam contagem ou bytes. Snippets respeitam até 512 bytes UTF-8 por lado,
sem cortar caracteres, e nunca substituem o valor integral na aplicação.

A política padrão permite exato aprovado e mantém fuzzy como sugestão. A política
conservadora explícita permite fuzzy com limiar pelo menos 0.98 e estrutura exata.
Como #161 não armazena assinatura estrutural portátil, a aplicação exige conferir
a unidade de origem no documento carregado, inclusive texto/digest/revisão, kind,
constraints, extensions, locale, contexto e termos protegidos. Unidades de outro
documento e markup ficam sem aplicação automática. Não se infere estrutura a
partir de uma fonte visível que pode ter perdido tags.

`plan_memory_pin(plan)` fixa pipeline (revisão e estágios), perfil (identidade
semântica completa) e glossário (referência imutável das regras efetivas ordenadas,
incluindo ausência explícita). Autores de revisões finais devem usar essas mesmas
referências do plano real. Valor final compatível pula o glossário; qualquer pin
divergente só produz sugestão `reviewed=False`, mantendo o estado original `approved`
como informação de origem. Nada deriva ou aprova uma revisão automaticamente.

`LegacyCacheCompatibility` é uma fonte separada, desligada por padrão. A opção
`--tm-legacy-cache-compatibility` exige `--translation-memory` e runtime rico.
A fonte usa somente linhas v2 com scope/kind/fingerprint e integridade completos;
linhas v1 sem estágio são excluídas, mesmo com opt-in. O limiar legado de 0.95 pode
ser utilizado nessa política explícita. Resultado de cache permanece
`machine_candidate`/`reviewed=False`; score ou reaplicação não mudam isso. O adaptador
não escreve no repositório governado. Sem rótulo de modelo, a CLI antiga continua
com suas semânticas históricas; os novos seletores são recusados nesse runtime.

`RetrievalDecision` distingue cache exato, memória aprovada, cache legado e provedor,
com chave completa de cache ou entrada/revisão, score, escopo, estado, confiança,
motivo e referência de proveniência sanitizada. `dataset_eligible` é sempre falso.
As decisões são entregues por unidade e persistidas no artefato de resultados,
com estágio explícito. Valores raw de cache/provedor/legado recebem o glossário
exatamente uma vez. Decisões desses valores continuam sem revisão após a derivação.
Artefatos que incluam recuperação são auditáveis, mas não são reutilizados como
raw: cada operação revalida aprovação/pins, inclusive após depreciação. O cache
exato operacional continua tendo precedência, independentemente do estado da TM.

Sugestões geram `memory_review_suggestions`, encaminhado ao CLI e contado no
relatório existente. Logs/eventos rotineiros contêm códigos, não valores, caminhos
ou segredos. A API autorizada devolve os valores completos e snippets para uma
futura visão de concordância. Esta issue não acrescenta editor de revisão, dataset,
consentimento ou migração de cache para memória aprovada.
