# Governança de direitos, finalidades, retenção e revogação

Status: Proposed

Policy-ID: `ayvu-rights-purpose-v1`

Vocabulary-Version: `1`

Decision-Date: `2026-11-05`

Owners: mantenedor do repositório Ayvu (aceite e produto); Policies (semântica);
Knowledge (cache/TM/corpus); Projects + Provenance (identidade e dependências);
Infrastructure (futura execução de retenção e exclusão).

Issue: [#165 — DESKTOP-134](https://github.com/MichaelAlexandreDev/ayvu/issues/165).

## Recomendação e limite desta decisão

Recomenda-se adotar os contratos e tabelas abaixo, com negação por padrão.
São recomendações de produto para revisão humana, não análise jurídica,
validação de titularidade ou licença para redistribuir conteúdo protegido.
Possuir uma obra não comprova direitos. O Ayvu apenas registra uma afirmação
local e uma escolha humana delimitada; não pode conceder direitos de terceiros.

Este registro permanece **Proposed**. Nenhum registry de direitos/finalidades
pode começar enquanto ele estiver Proposed. Não há registry, esquema de banco,
migração, CLI, UI, exportador, uploader, worker de exclusão, pipeline de treino
ou mecanismo de unlearning nesta entrega. As tabelas descrevem o contrato de
uma implementação futura, condicionado ao aceite e às issues de execução.
Não alteram comandos, mensagens, retenção nem permissões da CLI atual.

`RightsAssertion` registra que um ator local identificado afirmou uma base de
direitos para um escopo. Não valida a afirmação nem autoriza uma finalidade.
`PurposeGrant` é o **único registro canônico de autorização** para as finalidades
governadas, inclusive a recusa explícita. Exige uma afirmação de direitos
efetiva separada. Nenhum dos dois, sozinho, representa permissão universal.

“Consentimento” e o termo de produto “consent” descrevem somente o ato humano
explícito que pode criar um `PurposeGrant`. Não são terceiro objeto de domínio,
tipo serializado, alias de `PurposeGrant`, base de direitos, estado ou fonte
independente de autoridade. Campos como `consent`, `consent_id` e
`consent=true` são desconhecidos no contrato canônico e invalidam sua entrada;
não se convertem automaticamente em concessão.

## Evidência e dependências presentes

Verificação local sem rede: os três artefatos obrigatórios estão em `main`
`bca1ccf1b035dbab8542475368265e58b4e9853c`. O checkout analisado é
`aaca5668781996c9fa53497d48343cac6a537c7d`, que contém essa base. Presença em
`main` é o pré-requisito, não fechamento de issue nem aceite de outra proposta.

| Dependência / evidência | O que este registro consome e o que não presume |
| --- | --- |
| [#129](https://github.com/MichaelAlexandreDev/ayvu/issues/129), [modelo de ameaças](../product-threat-model.md) | Accepted em 2026-11-05; T03 (destinos), T05 (cache/corpus), T09 (AI/MCP), T11 (retenção/revogação), F06 e G04. Esse aceite não aceita esta política. |
| [#134](https://github.com/MichaelAlexandreDev/ayvu/issues/134), [manifesto](../project-manifest.md) | `project_id`, revisão e identidade exata da fonte/configuração. Caminho, título e hash isolado não são escopo nem autoridade. `policy_ref` não é uma concessão. |
| [#138](https://github.com/MichaelAlexandreDev/ayvu/issues/138), [DEC-014/proveniência](../provenance.md) | Referências opacas, revisões e linhagem; atores são tipos, sem nomes de pessoas. Sujeito opaco não prova identidade nem direitos; histórico copiado não autoriza. |
| [Limites de módulos](../modular-monolith-boundaries.md), [artefatos](../job-artifacts.md), [escopo oficial do produto](../../product-scope.md) | Policies decide; cache, TM e corpus são independentes. Um artefato existente não prova permissão. Conteúdo para corpus/treino não é contribuição automática. |
| [Contratos de providers](../provider-contracts.md), [descoberta](../provider-discovery.md) | Configuração/descoberta não são autorização de rede ou de finalidade. |

As referências GitHub são evidência de rastreabilidade, não foram consultadas
online nesta revisão. Nenhuma norma externa ou documento jurídico é pressuposto
por esta proposta. Futuras regras de rede de
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159), memória aprovada de
[#161](https://github.com/MichaelAlexandreDev/ayvu/issues/161) e gates G03/G04 do modelo
continuam necessários. A proveniência atual não oferece consulta de descendentes
nem exclusão individual, e a CLI não grava toda a linhagem. Sem inventário
completo, o uso governado futuro deve negar; esta proposta não preenche essas
lacunas com uma suposta garantia de exclusão.

## Convenções fechadas e versionamento

Todos os identificadores das tabelas são literais, sensíveis a maiúsculas e
versionados juntos por Policy-ID e Vocabulary-Version. Não há `other`, `all`,
curinga, propósito livre, extensão `x-`, valor inferido ou desconhecido aceito.
As palavras explicativas e IDs de cenários não são valores de política.
Listas são conjuntos finitos não vazios, sem duplicatas, em ordem lexical,
exceto as listas cujo mínimo zero é explicitado abaixo;
lista vazia fora dessas exceções, tipo errado, chave extra, chave duplicada e campo ausente são
inválidos. `null` só existe onde a tabela permite explicitamente.

Referências locais novas (`record_id`, `actor_ref`, `preview_ref`, `support_ref`,
`destination_ref`, `work_ref`, `segment_ref`, `snapshot_ref`, `obligation_ref`)
são UUID4 canônicos minúsculos, gerados aleatoriamente pela aplicação local.
Não são texto, URI, caminho, hash de conteúdo, nome de usuário ou documento.
O `project_id` existente é o UUID canônico do manifesto, sem exigir retroativamente
UUID4 a manifestos válidos. Referência opaca não é evidência por si só: deve
resolver univocamente no contexto local autorizado, sem resolução de rede.
IDs de exemplo abaixo são símbolos de revisão, não entradas serializáveis.

Tempos são UTC canônico `YYYY-MM-DDTHH:MM:SS.ffffffZ`; calendário válido,
precisão de microssegundos, sem arredondamento. Durações são segundos inteiros
positivos; um dia é exatamente 86.400 segundos. Booleanos não são inteiros.
Há um relógio autoritativo injetado pela aplicação, fora do payload/modelo.
Relógio indisponível ou retrocedendo em relação à última decisão local nega
novos efeitos até reconciliação local. O tempo da importação não é autoridade.

| Valor de versão / mudança | Transição e efeito obrigatório |
| --- | --- |
| `ayvu-rights-purpose-v1`, vocabulário `1` | Único par entendido por este registro; ainda bloqueado por Proposed. |
| Qualquer par diferente, ausente ou parcialmente reconhecido | `deny_version`; não mapear por similaridade, migrar automaticamente ou aceitar a parte conhecida. |
| Correção editorial sem mudar semântica | Mesmo par; revisão comum verifica que nenhum conjunto, prazo, consequência ou precedência mudou. |
| Adicionar/remover valor, mudar escopo, prazo, autoridade, consequência ou regra | Novo Policy-ID e nova Vocabulary-Version; novo registro Proposed e novo aceite humano. |
| Ativar nova versão aceita | Todas as afirmações e concessões anteriores ficam incompatíveis imediatamente. Não há migração em v1, nem substituição entre versões. Projetos com registros de outra versão ficam bloqueados; eventual migração exige outra decisão de governança aceita com regras explícitas, nunca um import ou novo grant que ignore o passado. |

Status do registro de governança tem conjunto fechado `Proposed`, `Accepted`,
`Superseded`. Só `Proposed -> Accepted` pelo gate final; só
`Accepted -> Superseded` ao registrar substituição aceita. Os demais movimentos
são inválidos. Uma nova versão começa em Proposed. Versão desconhecida de
impedimento não pode ser descartada por um leitor novo: projeto com registros
de outra versão resulta em `deny_version`. O par Policy-ID/Vocabulary-Version
é tratado em P06, não como valor desconhecido de um vocabulário em P01.

## Vocabulários de finalidade, dados e destino

| Finalidade exata | Significado e consequência a mostrar no preview |
| --- | --- |
| `corpus_build` | Selecionar/copiar conteúdo para corpus ou dataset governado local. Cria cópia independente; não permite exportar, treinar ou compartilhar. |
| `dataset_export` | Produzir um pacote de dataset para um destino exato. Cria cópia que pode escapar do controle local; não autoriza o destinatário a treinar ou publicar. |
| `model_training` | Usar dados para ajustar parâmetros, inclusive fine-tuning. O modelo pode reter informação; revogar não desfaz instantaneamente o aprendizado. |
| `model_evaluation` | Usar dados como entradas, referências ou benchmarks de avaliação. Resultados/logs também precisam de inventário e prazo; não permite treino. |
| `research` | Analisar conteúdo em uma atividade de pesquisa delimitada pelo snapshot/preview. Não permite exportar, treinar, avaliar modelos ou publicar por inferência. |
| `contribution` | Entregar dados a um destinatário privado para um acervo ou iniciativa identificada. Não é renúncia de direitos nem licença irrestrita. |
| `redistribution` | Disponibilizar cópia de dados ou artefato de modelo ao destinatário aprovado, inclusive publicação pública. Pode ser impossível recolher cópias; a afirmação local não licencia obra de terceiros. |

Finalidade não implica outra: avaliar durante treino exige as duas; exportar
para contribuir exige `dataset_export` e `contribution`; publicar o pacote exige
também `redistribution`; montar corpus para treinar exige `corpus_build` e
`model_training`. A requisição declara o conjunto completo da operação, e
nenhum campo de payload escolhe a classificação. Um uso ainda não classificado
nega. Não há finalidade “tradução” nesta lista: o fluxo operacional está na
tabela de operações, sem ser uma concessão para corpus.

| Classe de dados exata | Unidade e limite |
| --- | --- |
| `source_text` | Texto visível da fonte; nada de assets/DRM, metadados ocultos ou código. |
| `translation_text` | Texto traduzido isolado, ainda potencialmente protegido e identificável. |
| `aligned_pair` | Par fonte/tradução alinhado, tratado como unidade distinta; não se infere dos dois grants isolados. |
| `review_annotation` | Anotação humana de revisão, sem incorporar automaticamente texto do livro. Se incorpora, declarar também a classe de conteúdo correspondente. |
| `provenance_metadata` | Apenas referências e códigos allowlisted de linhagem, sem identidade pessoal ou caminho. Metadados continuam sensíveis. |
| `model_artifact` | Artefato de modelo derivado, com ancestralidade completa; não se presume anônimo nem livre de conteúdo memorizado. |

Par dividido ou dado transformado é nova requisição com suas classes exatas;
não existe autorização por conversão, anonimização alegada ou extensão de arquivo.
O snapshot de um par identifica separadamente ambos os componentes para checar
direitos. Dados não enumerados, incluindo assets, prompts genéricos, credenciais
e documentos de suporte, não são exportáveis por esta política.

| Classe de destino exata | Binding e limite |
| --- | --- |
| `local_managed` | Um armazenamento/runner gerido pelo Ayvu neste contexto local; não um arquivo externo arbitrário nem processo que encaminha dados. |
| `private_recipient` | Um destinatário não público específico e previamente registrado localmente; mesmo em loopback, se externo ao controle Ayvu. |
| `public_release` | Um canal público específico previamente registrado; acesso futuro e cópias são incontroláveis. |

Cada concessão tem exatamente uma classe e um `destination_ref` que resolve
para um descritor imutável de catálogo **local**, revisado por humano. Catálogo
e seu runtime são futuros; até existirem, a resolução falha e o uso nega.
O descritor vincula receptor, armazenamento/runner e versão de configuração;
não recebe URL, classe ou receptor de documento, manifesto importado, AI/MCP
ou requisição como fonte de autoridade. Mudar receptor/configuração ou
redirecionar exige nova referência e novo preview. Mesma classe com referência
diferente é destino diferente. A aplicação de rede verifica seus próprios
endpoints, redirects, proxies e modo offline; a concessão não os alarga.

| Finalidade | Classes de dados válidas | Classes de destino válidas |
| --- | --- | --- |
| `corpus_build` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed` |
| `dataset_export` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed`, `private_recipient` |
| `model_training` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed`, `private_recipient` |
| `model_evaluation` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `local_managed`, `private_recipient` |
| `research` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `local_managed`, `private_recipient` |
| `contribution` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `private_recipient` |
| `redistribution` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `private_recipient`, `public_release` |

A tabela é exaustiva: o produto cartesiano finalidade × dados × destino do
registro/requisição precisa ser subconjunto das células válidas. Qualquer
outra combinação inteira é inválida, sem aproveitar apenas a parte válida.
Uma operação com efeitos locais e externos se divide em requisições com destino
único; todas devem autorizar antes de cada efeito, sem aprovação parcial tácita.

## Escopos finitos e identidade

Todos os escopos incluem `project_id`, `manifest_revision` (inteiro positivo
exato) e `snapshot_ref`. O snapshot é imutável, localmente revisado, de 1 a
4.096 sujeitos atômicos; contém somente referências, versões e relações, nunca
texto. Nenhum escopo abrange sujeitos ou revisões futuros. Outro projeto,
clone/importação ou nova revisão do manifesto não herda autoridade. Até uma
revisão que apenas muda uma localização exige nova revisão explícita de escopo;
esta é a opção conservadora enquanto não existir contrato mais específico.

| `scope_kind` | Seleção adicional obrigatória | Cobertura exata |
| --- | --- | --- |
| `project` | `work_ref=null`, `segment_ref=null`, `subject_ref=null` | Conjunto finito do snapshot daquele projeto/revisão. |
| `work` | `work_ref`; `segment_ref=null`, `subject_ref=null` | Sujeitos do snapshot vinculados àquela obra/versionamento local. Não abrange outra tradução por semelhança. |
| `segment` | `work_ref`, `segment_ref`; `subject_ref=null` | Sujeitos do snapshot daquele segmento/revisão. Índice posicional de revisão não é identidade. |
| `subject` | `work_ref`, `subject_ref`; `segment_ref` conforme o mapping imutável (null apenas para sujeito sem segmento) | Um sujeito exato do snapshot, podendo representar um artefato com ancestralidade registrada. |

Para este contrato, `subject_ref` usa namespace fechado
`ayvu-rights-subject`, versão inteira `1`, valor UUID4 aleatório. É um mapping
local proposto para o `SubjectRef` opaco de DEC-014, não novo significado
retroativo para seus namespaces livres. O mapping referencia obra, segmento
quando houver, versão da identidade e linhagem; colisão, resolução múltipla,
linhagem truncada, identidade desconhecida ou ausência desse mapping negam.
`work_ref` e `segment_ref` identificam bindings imutáveis nesse snapshot, sem
inventar um ID universal de obra ausente em #134. O fingerprint permanece no
manifesto existente; não se copia seu hash para registro de política/tombstone.

Relação amplo/específico é inclusão real dos conjuntos resolvidos, e não ordem
de nomes nem contagem de campos. Dois escopos podem se sobrepor sem conter um
ao outro. Todas as interseções são calculadas antes da decisão. Uma requisição
com 4.097 sujeitos é inválida e precisa ser dividida, preservando checagem
completa e obrigações de cada parte. Não há truncamento que permita uso.

## Atores, origem e autoridade

| `actor_kind` / origem | Pode afirmar direitos? | Pode criar/reativar/ampliar grant? | Outras transições permitidas |
| --- | --- | --- | --- |
| `local_human` | Sim, por operação explícita e preview | Criar novo, inclusive substituição explícita; nunca reativar ID terminal | Recusar, retirar/contestar afirmação e revogar conforme tabelas. |
| `policy_engine` | Não | Não | Invalidar uso, calcular expiração e revogar por risco/mudança conforme razões; jamais conceder. |
| `project_import` | Não | Não | Somente dado candidato não autoritativo. |
| `document` | Não | Não | Somente conteúdo não autoritativo. |
| `ai_mcp` | Não | Não | Somente sugestão/saída não autoritativa. |
| `provider` | Não | Não | Somente metadado/resposta não autoritativa. |
| `copied_history` | Não | Não | Somente histórico não autoritativo. |

O `actor_ref` de `local_human` é pseudônimo aleatório deste contexto/projeto,
resolvido pela sessão local autenticada, não nome, e-mail, conta global ou valor
importado. A aplicação vincula uma operação explícita a uma sessão humana
presente; não basta receber `actor_kind=local_human`. Atores de proveniência
`human`/`system` não se convertem nesses poderes. O motor não é representante
legal do humano. Ausência de ator local verificável nega.

O preview humano obrigatório é imutável e vincula Policy-ID, vocabulário,
escopo/snapshot, bases declaradas, finalidades, classes, destino/receptor,
prazo de uso/retenção, impedimentos substituídos e todas as consequências e
limitações deste registro. O humano confirma cada dimensão sem seleção prévia
de novas finalidades. Mudança entre preview e efeito invalida a operação.
Um preview aceito para direitos não é aceite para finalidade: são operações
distintas, referências e evidências distintas, mesmo se a futura UI as agrupar.
Somente o serviço local de aplicação poderá emitir os IDs após confirmação;
campos serializados, replay de preview, histórico copiado e saída AI/MCP não
podem criar, reativar, ampliar ou personificar um registro local. Na ausência
de futura proteção de integridade/origem local, negar, sem confiar em um UUID.

## RightsAssertion: evidência, campos e ciclo de vida

| `basis` fechado | O que o ator afirma | `support_ref` |
| --- | --- | --- |
| `creator_asserted` | Criou o material selecionado e afirma deter os direitos relevantes. Não resolve direitos de terceiros incorporados. | null ou referência opaca |
| `permission_asserted` | Recebeu permissão/licença aplicável ao material selecionado. Ayvu não verifica termos nem documento. | Referência opaca obrigatória |
| `public_domain_asserted` | Afirma que o material selecionado é domínio público no contexto relevante. Não é certificação jurídica. | null ou referência opaca |

Não se deduz base de compra, cache, aprovação de TM, importação, termos do
produto ou envio a provider. Exceção legal não está representada em v1; não
inventar `other` ou converter incerteza em domínio público. Uma futura base
exige nova versão e revisão humana/jurídica apropriada.

Campos exatos de `RightsAssertion`: `record_id`, `policy_id`,
`vocabulary_version`, `scope`, `data_classes`, `basis`, `actor_kind`, `actor_ref`,
`asserted_at`, `expires_at`, `expiry_kind`, `support_ref`, `preview_ref`,
`state`, `replaces`, `terminal_at`, `terminal_reason`. `scope` segue a tabela;
`data_classes` tem 1 a 6 valores e não autoriza finalidade. `actor_kind` deve
ser `local_human`. `replaces` tem 0 a 64 IDs locais distintos. O mesmo limite
aplica ao grant; além disso, reconciliação explícita em etapas mantendo negação.

`expiry_kind` só aceita `fixed_utc`. Vale
`asserted_at < expires_at <= asserted_at + 31.536.000 s` (365 dias).
`terminal_at` e `terminal_reason` são null em `asserted`; nos estados terminais
são obrigatórios, com tempo não anterior à afirmação. Não se alteram base,
escopo, classes, ator, evidência ou validade de um ID existente.

| Estado armazenável | Transição permitida | Efeito |
| --- | --- | --- |
| `asserted` | Criação local; pode ir para `withdrawn` ou `disputed` | Candidata, nunca grant. Só efetiva dentro do prazo e versão. |
| `withdrawn` | De `asserted`, humano, razão `human_withdrawal` | Terminal, bloqueia o escopo/classes e revoga grants dependentes. |
| `disputed` | De `asserted`, humano ou motor, razão `rights_disputed` ou `identity_ambiguous` | Terminal, bloqueia; controvérsia não se resolve escolhendo outra base. |

Estados efetivos calculados adicionais, nunca gravados como estado de entrada:
`expired` quando `now >= expires_at`; `version_incompatible` quando o par de
versões difere. Também bloqueiam. Não há renovação nem reativação de ID
terminal. Nova afirmação com novo preview e novo ID só substitui IDs nomeados
de binding **idêntico** (escopo/snapshot e classes), sem apagar evidência de
retirada/controvérsia. O preview exige que o humano reconheça cada impedimento.
Substituição e criação são atômicas; falha mantém o bloqueio. Nenhuma das bases
tem precedência sobre outra. Mais de uma afirmação efetiva aplicável ao mesmo
sujeito/classe é `deny_ambiguous`, salvo repetição idempotente do mesmo ID e
mesmo conteúdo. Conteúdo diferente com mesmo ID é corrupção (`deny_invalid`).

Cada sujeito/classe da requisição exige exatamente uma afirmação válida que o
cubra; para `aligned_pair`, exige também cobertura dos componentes
`source_text` e `translation_text`; para `model_artifact`, cobertura do artefato
e de todos os ancestrais com conteúdo, segundo suas classes. Ancestral
desconhecido, truncado ou sem direitos nega. Afirmações podem ser de atores
locais distintos, mas nenhum substitui o ator humano da concessão.

## PurposeGrant: evidência, campos e ciclo de vida

Campos exatos de `PurposeGrant`: `record_id`, `policy_id`,
`vocabulary_version`, `scope`, `purposes`, `data_classes`, `destination_class`,
`destination_ref`, `rights_assertion_refs`, `actor_kind`, `actor_ref`,
`created_at`, `granted_at`, `expires_at`, `expiry_kind`, `retention_class`, `preview_ref`,
`state`, `replaces`, `terminal_at`, `terminal_reason`.

`purposes` contém 1 a 7 valores exatos; `data_classes` de 1 a 6.
`rights_assertion_refs` contém de 1 a 64 IDs locais de afirmações para um
`active`; não resolve importadas. Em `draft` ou `denied`, pode ser vazio,
pois negar não exige provar direitos. `actor_kind` é sempre `local_human`
na criação; ator/tempo de revogação ficam na evidência terminal separada.
Um grant nunca contém documentos jurídicos ou texto da fonte.

`created_at` é o instante de criação local e nunca muda. Para active/denied
criados diretamente, created_at=granted_at=confirmed_at do preview.
Em draft, granted_at é null e expires_at é o prazo proposto, limitado a
created_at+7.776.000 s; o draft é descartado após 86.400 s mesmo se esse prazo
for maior. Na confirmação de draft, granted_at recebe o tempo autoritativo;
não se pode ativar draft já descartável ou com prazo vencido. Em active/denied,
`expiry_kind=fixed_utc` e
`granted_at < expires_at <= granted_at + 7.776.000 s` (90 dias).
Para `active`, `expires_at` não ultrapassa a menor expiração de suas afirmações.
`retention_class=governed_copy_until_expiry` é o único valor para grants.
Recusa também tem prazo válido para o registro, mas **não perde seu bloqueio**
quando esse prazo passa. `terminal_at/reason` são null em `draft` e `active`,
obrigatórios em `denied` e `revoked`; recusa usa `human_refusal` e
terminal_at=granted_at, instante de confirmação da recusa, inclusive ao sair de
draft. Toda combinação finalidade/dados/destino segue a tabela exaustiva.

| Estado armazenável | Transições permitidas | Consequência em linguagem comum |
| --- | --- | --- |
| `draft` | Criar localmente; `draft -> active` ou `draft -> denied`, somente humano com novo preview atual | Rascunho não autoriza, não bloqueia outro registro por existir e não pode ser importado como autoridade. |
| `active` | Criar diretamente com confirmação ou sair de draft; `active -> revoked` | Permite somente o uso listado, enquanto todos os gates passarem; nunca uso universal. |
| `denied` | Criação ou saída de draft por recusa explícita | Não usar essa seleção para esse conjunto de finalidades/classes/destino; recusa prevalece mesmo contra grants mais específicos. |
| `revoked` | Somente de active, humano ou motor nas razões permitidas | Cessar novos usos agora; dependências são bloqueadas e recebem obrigação de retirada/exclusão. Cópias externas podem permanecer. |

Estados efetivos calculados adicionais: `expired` e `version_incompatible`,
com a mesma regra temporal/de versão das afirmações; não são entradas válidas.
Expirar equivale a cessar usos e emitir obrigação, não renovar. `active` não
pode voltar a draft; `denied`/`revoked` não voltam a active; nenhum ID é
reativado por importação, aprovação de TM, passagem do tempo ou versão nova.
Todos os movimentos não enumerados são inválidos e retornam `deny_invalid`.
Campos de active/denied são imutáveis salvo state/terminal_at/terminal_reason
na transição permitida. Durante draft -> active/denied só podem mudar state,
granted_at, preview_ref, rights_assertion_refs e campos terminais aplicáveis,
atomicamente na confirmação. Seleção, destino e prazo proposto não mudam;
mudá-los exige outro draft/ID. Um draft incompleto pode receber as referências
de direitos apenas nessa confirmação, que reavalia todas as condições.

Nova escolha exige novo ID/preview. `replaces` só retira da seleção efetiva
impedimentos explicitamente nomeados de binding **inteiramente idêntico**:
Policy-ID, escopo/snapshot, conjunto de finalidades, classes e destino/ref.
Não altera a história, obrigações existentes ou registros dependentes.
Substituição entre versões é inválida em v1; não há equivalência automática
nem operação de reconciliação interversão implícita. Dois sucessores
do mesmo registro são ambiguidade e bloqueiam. Substituição incompleta continua
negada. Um grant específico não cancela recusa ampla; para mudar essa escolha,
o humano precisa rever a recusa ampla inteira e criar depois as restrições
exatas desejadas. Não há exceção implícita ou escolha do registro mais recente.

## Operações e gates independentes

As linhas ordinárias abaixo continuam exigindo autorização normal de
projeto/arquivos, seleção humana e segurança de conteúdo. Não há validação legal
implícita. São classificação para preservar o produto atual, não nova permissão
para automatizar exportação ou compartilhar dados. Descoberta, aceite de termos
e uma URL permitida não concedem finalidade.

| Operação/classificação fechada | Gate de finalidade | Outros gates e limite |
| --- | --- | --- |
| `project_local` — criar/importar/inspecionar projeto, ler/extrair texto visível, gerar EPUB traduzido ou Markdown/revisão para uso do próprio projeto | Não exige PurposeGrant | Autorização ordinária e destino local do operador. Exportar um dataset não cabe nesta linha por mudar extensão ou nome. |
| `cache_local` — criar/consultar/reter/limpar cache operacional, importar/exportar seu intercâmbio para uso pessoal local | Não exige PurposeGrant | Cache separado; usar intercâmbio para pesquisa/corpus/entrega exige as finalidades governadas. Retenção atual não muda. |
| `tm_local` — criar/reusar TM pessoal ou aprovar TM no projeto | Não exige PurposeGrant desta política | Aprovação de qualidade não é base de direitos nem corpus. A #161 continua dona do ciclo de memória aprovada; nenhum reuso entre projetos é inferido. |
| `provider_operational` — enviar texto visível selecionado ao tradutor para produzir o resultado solicitado | Não exige PurposeGrant para esse processamento operacional | Autorização ordinária e de rede separadas, com receptor/limites explícitos. Termos/uso do provider não autorizam treino, avaliação, pesquisa ou revenda por ele. Se não puder separar esses usos, o futuro gate nega esse envio. |
| `governed_use` — qualquer uma das sete finalidades, mesmo inteiramente local | Exige RightsAssertion e PurposeGrant independentemente efetivos | Projeto, identidade, inventário/linhagem, finalidade e, se houver egress, rede devem passar. Exportação/distribuição/contribuição nunca se disfarçam de cache ou saída ordinária. |

Operação desconhecida é `deny_unknown`. A classificação vem de um caso de uso
local revisado, não de texto de livro, extensão de arquivo, nome de ferramenta
ou propósito declarado pelo solicitante não confiável.

| Autorização ordinária/identidade | Finalidade (quando exigida) | Rede (quando há egress) | Resultado |
| --- | --- | --- | --- |
| Não | Qualquer | Qualquer | `deny_project` |
| Sim | Não | Qualquer | Negar pelo motivo de finalidade |
| Sim | Sim | Não | `deny_network` |
| Sim | Sim ou dispensada por operação ordinária | Sim ou sem egress | Somente então permitir o efeito delimitado |

Não há socket “local” dispensado do gate de rede. `local_managed` só dispensa
rede se nenhum transporte ocorre. A permissão de rede nunca supre finalidade;
um PurposeGrant nunca amplia destino, modo offline ou autorização do provider.

## Algoritmo e tabelas completas de política efetiva

O resultado fechado é `allow` ou `deny` com exatamente um dos códigos abaixo.
A ordem P00–P18 fixa o motivo quando há vários erros; **todas** as condições
precisam passar. Não se continua após erro para procurar um grant mais
permissivo. Nas tabelas de conflitos/cenários, pressupõem-se os gates anteriores
satisfeitos, exceto quando explicitamente indicado. Como este registro ainda
está Proposed, qualquer tentativa real governada para v1 para em P00.

A aplicação classifica a operação antes desta avaliação. P00 e P05–P16 só
se aplicam a `governed_use`. Operações ordinárias percorrem somente P01
(identificador de operação), P02 (estrutura própria), P03 (autorização/identidade
ordinária), P04, P17 e P18; não recebem campos de grant/assertion nem ficam
bloqueadas por Proposed. Essa distinção não muda a CLI entregue.

Uma requisição normalizada Q contém operação, projeto/revisão, sujeitos/classes,
conjunto completo de finalidades, destino exato e instante do relógio.
“Aplicável” significa interseção não vazia de sujeitos **e** de finalidades
**e** de classes, com o mesmo destino/classe. Para afirmações, não há dimensão
finalidade/destino. Conjunto impossível de resolver não é conjunto vazio.
Registros autoritativos malformados que não permitam provar disjunção bloqueiam
todos os usos governados do projeto até reconciliação. Dados importados ficam
fora do conjunto autoritativo, sem poder criar bloqueios arbitrários no registro.

| Ordem | Condição avaliada | Resultado / código |
| --- | --- | --- |
| P00 | Registro não Accepted, aceite incompleto ou versão ativa sem aceite próprio | `deny / deny_governance` |
| P01 | Operação, finalidade, classe, destino, scope_kind, ator, base, estado, retenção, expiry_kind ou razão desconhecidos | `deny / deny_unknown` |
| P02 | Estrutura inválida, combinação inválida, relógio ausente/retrocedendo, ID com conteúdos diferentes, substituição inválida ou origem local sem integridade | `deny / deny_invalid` |
| P03 | Projeto sem autorização ordinária, identidade/snapshot ausente, ambíguo, obsoleto, truncado ou fora do binding; inventário terminal pendente/incompleto | `deny / deny_project` |
| P04 | Operação ordinária corretamente classificada | Dispensar P05–P16; ainda aplicar P17 e P18. Não criar grant/assertion. |
| P05 | Tentativa de usar import, documento, AI/MCP, provider, ator forjado ou histórico copiado como autoridade, ou preview ausente/não vinculado | `deny / deny_authority` |
| P06 | Par de versões incompatível/ausente em registro necessário ou impedimento não reconciliado | `deny / deny_version` |
| P07 | Afirmação aplicável withdrawn/disputed, expirada ou substituição bifurcada; duas afirmações efetivas por sujeito/classe | `deny / deny_rights` para retirada/disputa/expiração; `deny / deny_ambiguous` para multiplicidade/bifurcação |
| P08 | Falta exatamente uma afirmação efetiva para qualquer sujeito/classe/componente/ancestral de Q | `deny / deny_rights`; ainda não examina refs de um grant. |
| P09 | PurposeGrant aplicável `denied` ou `revoked` não explicitamente substituído | `deny / deny_explicit` ou `deny / deny_revoked`, respectivamente; se ambos, `deny_explicit` |
| P10 | PurposeGrant active aplicável já expirou, ainda que haja outro active válido | `deny / deny_expired`; não voltar a grant antigo/amplo |
| P11 | Mais de um active candidato cobrindo Q inteira, ou sucessores conflitantes | `deny / deny_ambiguous`; duplicata idempotente do mesmo ID/conteúdo conta uma vez |
| P12 | Nenhum grant autoritativo não-draft existente para o projeto | `deny / deny_absent` |
| P13 | Nenhum único active restante cobre todos os sujeitos/revisões de Q | `deny / deny_scope` |
| P14 | Nenhum desses grants cobre todas as finalidades de Q | `deny / deny_purpose` |
| P15 | Nenhum desses grants cobre todas as classes de Q | `deny / deny_data` |
| P16a | Nenhum desses grants cobre o destino/classe/ref exatos de Q, ou descritor/receptor não resolve | `deny / deny_destination` |
| P16b | O único candidato integral não referencia todas as afirmações de P08, ou referencia afirmação não efetiva | `deny / deny_rights` |
| P17 | Há egress sem autorização independente de rede para o destino/configuração atuais | `deny / deny_network` |
| P18 | Todos os gates passaram; exatamente um grant cobre Q inteira quando exigido | `allow / allow_bounded` |

P13–P16a filtram sequencialmente o mesmo conjunto de candidatos, sem combinar
permissões de registros diferentes. Um active “restante” também exige
`granted_at <= now < expires_at`, preview e origem válidos;
data futura é P02. Draft nunca é candidato ou impedimento; se só existem drafts,
P12 nega. Afirmação com data futura também é P02. Grant não aplicável por outro
propósito/dado/destino não bloqueia um grant válido de Q; simplesmente não
autoriza Q. Registros terminais/expirados só bloqueiam sua interseção, sem
revogar outras escolhas por inferência. Mudança de versão pendente é a exceção
conservadora explicitada em P06.

Para P11, “cobre Q inteira” é somente cobertura estrutural de todos os sujeitos,
finalidades, classes e destino; não depende do teste de refs de P16b. Se houver
mais de um, P11 nega antes de escolher qualquer um. P08 examina somente direitos
de Q, mesmo sem grant. Se não houver direitos nem grant, P08 é o primeiro
motivo; V02 e os casos destinados a testar ausência de grant pressupõem A válida.
A recusa continua válida sem direitos, mas P08 precede P09 no código devolvido:
sem A e com denied, o resultado é deny_rights; com A válida, deny_explicit.
Ambas as situações negam, sem apagar a recusa.

| Registros que se intersectam em Q | Decisão determinística |
| --- | --- |
| Allow amplo + deny específico | `deny_explicit` na interseção; fora dela avaliar todos os gates. |
| Deny amplo + allow específico | `deny_explicit`; especificidade não ultrapassa recusa. |
| Allow amplo + revogação específica, ou inverso | `deny_revoked`; não há fallback amplo/específico. |
| Allow válido + allow expirado aplicável, amplo/específico ou parcialmente sobreposto | `deny_expired`; um novo grant não limpa expiração anterior sem substituição explícita. |
| Dois allows cobrindo Q, amplo/específico, mesmo instante ou instantes diferentes | `deny_ambiguous`; nunca escolher newest, first ou most-permissive. |
| Dois allows apenas parciais cuja união cobriria Q | Negar no primeiro filtro P13–P16 que falhar; não costurar escopo/finalidade/dado/destino. |
| Um allow cobre Q e outro só parte, ambos válidos | Permitir se todos os demais gates passarem; o parcial não amplia nem decide Q. |
| Deny/revogado/expirado em escopo disjunto, finalidade/dado sem interseção ou outro destino | Não autoriza nem veta Q; verificar grant próprio de Q. |
| Mesmo ID, mesmo conteúdo repetido | Uma ocorrência idempotente; não cria autoridade se veio de importação. |
| Mesmo ID com conteúdos diferentes, ou origem ambígua | `deny_invalid`; nunca desempatar por timestamp. |
| Grant novo com replaces completo, binding idêntico e confirmação local; nenhum outro impedimento | Antecessores deixam de decidir novos usos; história e obrigações ficam. Reavaliar P00–P18 inteiro. |
| Grant novo sem replaces ou com binding diferente | Não limpa impedimento; substituição declarada inválida é P02. |

Para requests de múltiplos sujeitos, um deny em um deles nega Q inteira; o
chamador pode propor outra requisição sem esse sujeito, nunca omiti-lo
silenciosamente. A afirmação e o grant são checados no planejamento, antes de
cada leitura/copiação/egress e antes de publicar cada artefato. A decisão não
é um token ilimitado: revogação entre etapas impede o próximo efeito. Efeito
irreversível já iniciado é registrado como tal e segue as limitações abaixo.

## Revogação e obrigações limitadas

`terminal_reason`/`revocation_reason` usam exatamente os valores abaixo.
Uma razão de retirada de direitos não pode ser usada como recusa inicial de
grant sem registrar a transição correta. Ator de revogação é verificado na
operação local; seu campo serializado não basta.

| Razão | Ator permitido / origem | Efeito e consequência antes da confirmação |
| --- | --- | --- |
| `human_refusal` | local_human; criação de denied | Bloqueia novas escolhas sobrepostas; não é prova de que usos anteriores foram ilegais. |
| `human_withdrawal` | local_human; assertion withdrawn ou grant revoked | Deixar de usar agora; cópias derivadas geridas devem ser retiradas; terceiros podem já tê-las. |
| `rights_disputed` | local_human ou policy_engine; assertion disputed e grants dependentes revoked | Direitos incertos bloqueiam, sem julgamento jurídico; não resolver com uma afirmação mais ampla. |
| `identity_ambiguous` | local_human ou policy_engine; assertion disputed e grants dependentes revoked | Escopo/linhagem não confiável bloqueia até nova revisão de identidade. |
| `destination_changed` | local_human ou policy_engine; grant revoked | Não enviar ao receptor alterado; repetir preview para o destino novo, sem fallback. |
| `policy_changed` | policy_engine ou local_human; grant revoked | Antigo aceite não vale na versão nova; reconfirmar também os limites/impedimentos, sem conversão automática. |
| `security_incident` | local_human ou policy_engine; grant revoked | Parar futuros efeitos e isolar derivados geridos; não prometer desfazer vazamento anterior. |
| `expiry_reached` | policy_engine; estado efetivo expired de assertion/grant | Prazo acabou, novos usos negados e obrigações emitidas. Não gravar como revoked só para esconder expiração. |
| `explicit_replacement` | local_human; evento replaced de assertion/grant | Nova decisão com binding idêntico retira o antecessor da seleção, sem reativar ID nem apagar obrigações. |

Revogação confirmada em T é efetiva para todo efeito com relógio `now >= T`;
expiração também no instante exato (`now == expires_at`). Não depende de worker,
restart, próxima sincronização ou exclusão física. A operação registra a
evidência/obrigação durável antes de confirmar sucesso; falha de registro nega
uso e não finge que concluiu a revogação. Retirada/disputa/expiração de uma
afirmação invalida todos os grants que dela dependem imediatamente. Novos
rights/grants não ressuscitam datasets/artifacts já retirados.

Para cada evento terminal, o futuro executor inventaria dependentes e emite
uma obrigação idempotente por evento+artefato+tipo e, quando houver,
destinatário conhecido. Recusa inicial sem dependentes gera zero obrigações;
não inventar artefatos ou apagar cópias não relacionadas. Não cria worker nesta decisão. Limites
obrigatórios: bloquear seleção/treino/egress dos derivados já em T; inventariar
dependentes/linhagem em até 86.400 s; remover das seleções e excluir cópias
geridas até T+604.800 s (7 dias); enviar pedido aos destinatários conhecidos em
até 86.400 s; registrar resposta ou ausência dela até T+2.592.000 s (30 dias).
Inventário incompleto nunca é sucesso: manter o projeto/derivados bloqueados
e reportar obrigação `unfulfilled`. Sistema offline ou desligado não ganha
prazo novo: ao voltar, bloquear antes de qualquer efeito e reportar atraso.

| `obligation_kind` fechado | Objeto afetado e conclusão honesta |
| --- | --- |
| `inventory_check` | Um evento terminal sem inventário completo: bloquear o projeto, concluir enumeração até T+86.400 s ou registrar inventory_incomplete. Não exige inventar artifact_ref. |
| `managed_delete` | Dataset, pacote, avaliação, resultado de pesquisa ou cópia gerida derivada: bloquear e excluir no prazo de 7 dias. Falha fica explícita. |
| `recipient_withdrawal` | Destinatário conhecido: pedir cessação/exclusão; confirmação do destinatário não prova erasure físico. Sem controle/resposta, registrar limite. |
| `model_restrict` | Modelo derivado gerido: bloquear novo uso/exportação agora e eliminar a cópia gerida até 7 dias; decidir retreino em outra issue, sem prometer unlearning. |

| `obligation_state` fechado | Transição permitida / significado |
| --- | --- |
| `pending` | Criado no instante real da emissão >=T; somente `pending -> fulfilled` ou `pending -> unfulfilled`. due_at/delete_at continuam ancorados no T original, inclusive retries. |
| `fulfilled` | Terminal: ação gerida verificada ou pedido/resposta limitada registrado; não implica erasure externo. |
| `unfulfilled` | Terminal: prazo vencido, inventário incompleto ou ação impossível; incidente continua visível. Nova tentativa usa novo ID vinculado ao anterior, sem apagar falha. |

O campo fechado `obligation_result` é `pending`, `managed_deleted`,
`recipient_confirmed`, `recipient_unverifiable`, `model_removed`,
`deadline_missed`, `inventory_incomplete`, `managed_unavailable`, `inventory_complete`. `pending` só com pending;
fulfilled só com `managed_deleted` para managed_delete, `model_removed` para
model_restrict, `recipient_confirmed` para recipient_withdrawal ou
`inventory_complete` para inventory_check;
unfulfilled só com `deadline_missed` para qualquer tipo, `inventory_incomplete`
para inventory_check,
`managed_unavailable` para managed_delete/model_restrict, ou
`recipient_unverifiable` para recipient_withdrawal. Demais combinações inválidas.
Não há estado “resolvido” que esconda dependência desconhecida.
Fechamento usa closed_at real e pode ser tardio; depois do prazo nunca fulfilled.
No retorno de aplicação desligada, registrar unfulfilled/deadline_missed sem
alterar due_at/T. Se a enumeração mostrou lacuna dentro do prazo, usar
inventory_incomplete. Há uma inventory_check por evento cujo inventário não
possa ser comprovado completo em T; se comprovadamente sem dependentes, não
criar obrigação artificial e marcar inventário complete.

Cache operacional, TM pessoal/aprovada e original não são alvos automáticos
dessa exclusão: grant não altera sua retenção. Remover um derivado governado
que referencia uma entrada TM não apaga a entrada TM. Direitos contestados
podem exigir ação separada nesses sistemas pela sua própria política; esta
proposta não autoriza deletar fonte/EPUB original nem alterar texto visível.

Limites a mostrar **antes** de qualquer confirmação futura: exclusão local
não é secure erase; backups, exportações não geridas, destinatários e cópias
já distribuídas podem persistir; não há garantia de exclusão retroativa em
terceiros, recolhimento público ou unlearning instantâneo. Artefatos de modelos
já entregues não podem ser apagados remotamente por esta política. A obrigação
é cessar novos usos controlados, retirar derivados geridos e pedir retirada
onde houver receptor conhecido; não prometer controlar o que não se controla.

## Retenção, minimização e tombstones

Prazos propostos são tetos de produto para aceite humano, sem alegar obrigação
legal. Nenhum prazo infinito nem “reter conforme necessário” é permitido.
Não existe legal hold implícito: uma exigência legal futura precisa decisão
separada, sem inserir documentos privados aqui ou ampliar automaticamente TTL.

| `retention_class` fechado | Objeto e início do prazo | Expiração/regra exata |
| --- | --- | --- |
| `policy_source` | RightsAssertion/PurposeGrant e preview allowlisted | Manter enquanto candidato/draft válido; draft expira 86.400 s após criação e é descartado. Em terminalidade, expiração ou substituição T, apagar corpo/preview até T+2.592.000 s (30 dias), preservando tombstone e evidência/obrigações somente nos respectivos prazos. |
| `support_link` | Referência opaca de suporte da afirmação | Usada somente enquanto assertion candidata; depois só para revisão de retirada, apagar até T+2.592.000 s. Nunca armazenar documento, caminho ou credencial; não controlar a retenção do documento externo. |
| `revocation_evidence` | Evento terminal, refs e obrigações allowlisted | Apagar corpo detalhado até T+7.776.000 s (90 dias), mesmo com obrigação sem cumprimento; reduzir resultado final à tombstone. Não estender por tentativa/restart. |
| `audit_tombstone` | Metadados mínimos de terminalidade/decisão, começando em T | Apagar até T+31.536.000 s (365 dias), inclusive ator pseudônimo e refs de dependência. Recusa deixa de ter registro, mas ausência continua negando; não ressuscitar ID ou importar história. |
| `governed_copy_until_expiry` | Cópia/dataset/resultado/modelo governado | Prazo máximo igual ao menor expires_at de grants e assertions ancestrais; cessar uso nesse instante, excluir geridos até +604.800 s. Renovação não prolonga cópia existente sem nova revisão e nova identidade de derivado. |

`terminalidade T` é o primeiro de: retirada, disputa, revogação, expiração,
substituição ou mudança de versão. Nunca se recalcula T pelo último acesso.
Para denied, T é a confirmação da recusa (granted_at), não a criação prévia de
um draft; não emitir nova terminalidade nem
reiniciar TTL ao passar expires_at. Expiração, incompatibilidade e substituição
mantêm o estado armazenado original, mas geram evento/tombstone e bloqueio
efetivo; terminal_at/reason do corpo continuam null quando a tabela assim exige.
As razões calculadas são, respectivamente, expiry_reached, policy_changed e
explicit_replacement. Eventos created pertencem à fonte (policy_source) e são
apagados com ela até T+30 dias; não têm TTL de 90 dias iniciado antes de existir T.
Apagar uma política inteira exige antes revogar grants ativos e emitir
obrigações. Não expurgar bloqueios mais cedo para obter allow. Depois de
apagar tombstone, nenhum grant anterior a T é reutilizável: novo uso exige novo
preview/assertion/grant e identidade nova. Caso haja autorização concorrente
afetada por impedimento a expurgar, invalidá-la antes do expurgo, não permitir
fallback. A implementação futura precisa provar isso, inclusive restauração
de backup; impossibilidade de provar integridade após restore nega projeto.

O corpo terminal é reduzido a tombstone até 30 dias, **preservando o impedimento
exato** na allowlist abaixo até seu TTL de 365 dias. Não apagar o conjunto
finito de sujeitos necessário à interseção em P07/P09/P10. Para corrigir
identidade que não permita resolver equivalência do snapshot, v1 nega o projeto:
não permite substituir por escopo diferente nem inventa migração. O produto
deve encaminhar esse caso a decisão de identidade/política separada.

Não se guarda log permanente de cada leitura ou cada negativa. Durante uma
operação, motivo de decisão pode existir em memória com TTL até o fim dessa
operação, no máximo 86.400 s. Auditoria persistente somente para criação,
recusa, retirada, disputa, revogação, expiração, substituição e cumprimento de
obrigação, nos tetos acima. Rótulos de exibição, identidade real do operador,
endereço de receptor e detalhes de preview não entram em tombstone.

Allowlist exata de evidência de evento: `event_id` (UUID4), `record_id`,
`policy_id`, `vocabulary_version`, `event_kind`, `effective_at`, `actor_kind`,
`actor_ref` (null apenas para motor), `reason` (null só na criação),
`obligation_refs` (zero IDs, exceto exatamente um em obligation_closed), `retention_class`,
`delete_at`. `created` usa retention_class=policy_source; delete_at é null
enquanto fonte ativa não tem T, preenchido atomicamente em T para T+30 dias.
Para draft, é created_at+86.400 s; ativar draft atualiza para null até T.
Os demais usam revocation_evidence, com delete_at=T+7.776.000 s.
Obrigações apontam ao event_id terminal; ele não contém cópia da lista de
dependentes. A consulta completa por evento alimenta os contadores mínimos;
consulta parcial mantém inventory_state=incomplete, nunca conta como sucesso.
`event_kind` fechado: `created`, `denied`, `withdrawn`, `disputed`, `revoked`,
`expired`, `version_invalidated`, `replaced`, `obligation_closed`. Transições correspondem somente às
tabelas de ciclo de vida; expired é fato calculado. `obligation_closed` tem
exatamente uma obligation_ref e reason igual à razão terminal do evento pai;
estado/resultado são lidos da obrigação vinculada, não são campos implícitos
do evento. O evento pai é o event_id já existente da obrigação, nunca o próprio
obligation_closed; ciclos e referência inexistente são P02.
Cada um dos demais eventos usa a razão correspondente da tabela.
version_invalidated usa policy_changed, sem mudar estado armazenado da assertion;
grants também podem ter revoked/policy_changed. O primeiro evento **terminal** fixa T.
Não gravar dados livres em `reason`.

Allowlist exata de obrigação: `obligation_ref`, `event_id`, `artifact_ref`
(UUID4 de mapping local; null só para inventory_check), `destination_ref`
(UUID4 obrigatório só para recipient_withdrawal; null nos demais),
`obligation_kind`, `obligation_state`, `obligation_result`, `created_at`,
`due_at`, `closed_at` (null só em pending), `previous_ref` (null ou UUID4),
`sent_at`, `response_at`, `receipt_ref`, `delete_at`. Os três campos novos são
null para managed_delete/model_restrict/inventory_check. Para recipient_withdrawal, sent_at é
null até o envio ou o tempo autoritativo do pedido; response_at é null até a
resposta, senão >= sent_at. Resposta e fechamento são registrados atomicamente,
com response_at <= closed_at; não há response_at em pending. receipt_ref é UUID4
aleatório de confirmação local desta própria obrigação, sem objeto de suporte
adicional nem resolução externa (nunca resposta/texto/endereço). É emitido pelo
adaptador local ao observar a confirmação e apagado com a obrigação em T+90 dias;
obrigatório para recipient_confirmed
com sent_at/response_at não null, e opcional para recipient_unverifiable.
Cumprimento exige envio até T+86.400 s; atraso é deadline_missed, mesmo com
confirmação posterior. inventory_check usa due_at=T+86.400 s;
managed_delete/model_restrict usam due_at=T+604.800 s;
recipient_withdrawal usa due_at=T+2.592.000 s, com envio até T+86.400 s.
Seu corpo segue revocation_evidence (90 dias), e resultado mínimo segue tombstone.

Allowlist exata do tombstone: `record_id`, `record_kind`, `policy_id`,
`vocabulary_version`, `project_id`, `event_kind`, `terminal_reason`,
`effective_at`, `actor_kind`, `actor_ref` (null para motor), `preview_ref`,
`restriction`, `block_kind`, `inventory_state`, `obligation_summary`, `retention_class=audit_tombstone`, `delete_at`.
`record_kind` só `rights_assertion` ou `purpose_grant`.
`block_kind` só `rights`, `explicit`, `revoked`, `expired`,
`version_incompatible`, `none`: direitos withdrawn/disputed usam rights;
grant denied usa explicit; grant revoked usa revoked; expiração usa expired;
mudança de versão usa version_incompatible; replaced usa none. Substituição
explícita atualiza atomicamente também o block_kind do antecessor para none e
restriction para null, sem mudar T/delete_at nem o fato/razão terminal original.
Esse none persiste após apagar ambos os corpos, sem ressuscitar a recusa.
`restriction`
é null só para none; nos demais casos tem exatamente `scope`, `subject_refs`,
`purposes`, `data_classes`, `destination_class`, `destination_ref`, com os
mesmos valores imutáveis do binding e a lista de 1 a 4.096 sujeitos resolvidos
do snapshot. Para rights_assertion, purposes é vazio e ambos campos de destino
são null; para purpose_grant, são obrigatórios conforme grant original. Esta é
a informação mínima para continuar calculando interseção/substituição sem
guardar preview completo, evidência de suporte ou conteúdo. A lista de sujeitos
e relações de escopo necessária fica sujeita ao mesmo TTL de tombstone, não ao
TTL de conteúdo da fonte. Não existe outro índice permanente de bloqueio.
`inventory_state` fechado é `pending`, `complete`, `incomplete`, derivado da
inventory_check; só complete libera P03. Sem dependentes comprovadamente, é
complete com zero obrigações. Pending/incomplete bloqueiam o projeto inteiro,
inclusive se block_kind=none; substituir grant não sana inventário desconhecido.
Expiração de assertion em tombstone (record_kind=rights_assertion, block_kind=expired)
é P07 deny_rights; expiração de grant é P10 deny_expired, não se confundem.
`obligation_summary` é tupla dos três contadores inteiros não negativos
`pending_count`, `fulfilled_count`, `unfulfilled_count`, cada um <= 2**63-1;
sem obrigação, todos zero. Tombstone de criação só após terminalidade, com
event_kind terminal correspondente; não retém corpo integral do grant.
delete_at é sempre effective_at+31.536.000 s; effective_at usa o primeiro T,
não a última obrigação. Nenhum campo opcional fora das nulabilidades acima.

P03 exige **prova positiva de inventário completo atual em cada operação
governada**, não ausência de tombstone. Após o TTL de evidências de 90 dias,
resolver inventory_state=incomplete exige reconstrução integral local; sem ela,
continua negado. Antes de expurgar tombstone em 365 dias com inventário ainda
pendente/incompleto, invalidar todos os grants do projeto e descartar o contexto
de autorização governada (sem apagar o projeto ordinário ou o original).
O próximo acesso é bootstrap sem autoridade: inventário completo reconstruído,
novas afirmações, novos grants e novos previews são obrigatórios. Se não puder
reconstruir, P03 continua negando por falta da pré-condição positiva, sem manter
um marcador eterno. A implementação futura deve provar esse bootstrap também
após restore, não inferir complete do desaparecimento de registros.

Tombstones **não autorizam** e não recompõem grants. Retêm somente o binding
necessário a negar, não texto, título, hashes de conteúdo, endereço/URL, dados
da pessoa ou suporte. P07/P09/P10 leem os impedimentos reduzidos da mesma forma
que seus antecessores. O bloqueio concorrente é invalidado antes do expurgo
final para impedir fallback de grants antigos.
Registro mínimo de uma obrigação encerrada conserva só contadores;
referências da obrigação desaparecem em 90 dias. O binding de restrição pode
reter destination_ref e sujeitos até 365 dias exclusivamente para negar;
continuam metadados sensíveis, sem nome/endereço e sem anonimização alegada.

Proibições de privacidade valem para policy, preview persistido, eventos,
erros, repr, logs e tombstones: nenhum texto fonte/traduzido, prompt de livro,
documento jurídico de suporte, credencial, caminho privado absoluto, nome,
e-mail, título ou identificador pessoal desnecessário. Não derivar referência
de trecho, título, e-mail ou segredo por hash; digest não é anonimização.
O preview visual pode mostrar ao humano a seleção que ele já pode ler, mas seu
registro persistido só contém a allowlist, nunca uma captura de tela/texto.
Allowlist exata do preview persistido: `preview_ref`, `record_kind`, `binding`,
`actor_ref`, `issued_at`, `confirmed_at`, `consumed_at`, `confirmation_ref`,
`policy_id`, `vocabulary_version`, `expires_at`, `delete_at`. binding de
rights_assertion tem exatamente scope, data_classes, basis, support_ref,
actor_kind, actor_ref, expiry_kind, expires_at e replaces. binding de
purpose_grant tem exatamente scope, purposes, data_classes, destination_class,
destination_ref, rights_assertion_refs, actor_kind, actor_ref, expiry_kind,
expires_at, retention_class e replaces. São os campos homônimos deste contrato,
todos imutáveis no preview. Os nomes de campo nesta frase são literais.
IDs e asserted_at/granted_at são associados pelo serviço somente na confirmação,
que grava a preview_ref única no registro; uma preview_ref não pode criar dois
registros. asserted_at/granted_at devem coincidir com confirmed_at.
confirmation_ref é UUID4 de evidência da sessão local humana, sem identidade
real; não é aceita de payload importado. confirmed_at/consumed_at/confirmation_ref
são null antes da confirmação; confirmação explícita de **todas** as dimensões
e consequências preenche os três atomicamente e consome o preview uma única
vez, sem replay. issued_at <= confirmed_at=consumed_at < expires_at,
expires_at=issued_at+900 s (15 minutos); expirar preview não estende prazo do
grant. delete_at é null para preview consumido ligado a registro ainda sem T;
é fixado atomicamente em T para T+30 dias. Preview de draft segue created_at+1 dia.
Na emissão, delete_at=expires_at+86.400 s; ao consumir, aplicar atomicamente a
regra do registro vinculado acima. Preview expirado não consumido é apagado
até expires_at+86.400 s. Consequências são as desta versão, sem campo livre,
captura do receptor, texto ou documento. A prova local do ato ainda exige a
implementação/validação antireplay indicada nas questões abertas; até ela
existir, P05 nega.

## Exemplos adversariais e oráculos para revisão/teste

Esta matriz é o índice exaustivo para revisão dos vocabulários. Valor conhecido
não significa allow: só elimina P01; as demais condições continuam necessárias.

| Vocabulário / conjunto fechado | Validação e ponto de decisão |
| --- | --- |
| Policy-ID, Vocabulary-Version, status de governança | P00/P06; nenhuma tradução implícita entre versões. |
| Finalidades (7), classes de dados (6), classes de destino (3) | P01/P02 para cada célula válida/inválida do produto 7×6×3; P14/P15/P16a para correspondência exata. |
| scope_kind (4), namespace/versão de sujeito | P01/P02/P03; inclusão/interseção em P07–P13; identidade ausente não é escopo vazio. |
| actor_kind/origem (7), bases de direitos (3) | P01/P02/P05/P07/P08; só local_human cria, nenhuma base tem precedência. |
| Estados armazenados de assertion (3) e grant (4) | P02 para transições; P07/P09; draft não autoriza. |
| Estados efetivos expired/version_incompatible (2) | P06/P07/P10, derivados exclusivamente de versão/relógio, nunca entrada. |
| expiry_kind (1), retention_class (5) | P01/P02; prazos/fonte/TTL na tabela de retenção; retention_class de grant só governed_copy_until_expiry. |
| Razões terminais (9), event_kind (9) | P01/P02; combinações e atores da tabela de razões, ciclo de vida e evidência. |
| obligation_kind (4), obligation_state (3), obligation_result (9) | P01/P02; prazos, combinações e estado terminal na tabela de obrigações. |
| record_kind (2), block_kind (6), inventory_state (3), contadores/refs de tombstone | P01/P02; inventário P03; direitos/expired de assertion P07, explicit/revoked P09, expired de grant P10, version_incompatible P06, none nunca autoriza. |
| Operações (5), resultados (2), códigos de decisão (18) | Classificar antes de P00; P00–P18 e tabela de gates; qualquer campo obrigatório faltante nega. |

As únicas arestas de mudança de estado armazenado são as abaixo. Criação e
estado efetivo calculado não são arestas de reativação. Qualquer par de estados
não listado, inclusive auto-transição, é inválido em P02; retry idempotente
do mesmo evento não é nova transição.

| Ciclo | Arestas exatas permitidas |
| --- | --- |
| governança | `Proposed -> Accepted`, `Accepted -> Superseded` |
| RightsAssertion | `asserted -> withdrawn`, `asserted -> disputed` |
| PurposeGrant | `draft -> active`, `draft -> denied`, `active -> revoked` |
| obrigação | `pending -> fulfilled`, `pending -> unfulfilled` |

Símbolos: Q é uma requisição exata válida; A uma afirmação local; G um grant;
B/S escopos amplo/específico com inclusão comprovada; T é relógio injetado.
Não há texto de obra, pessoa real, endereço, segredo ou caminho nos exemplos.
Os testes de governança são documentais; não se afirma comportamento executável
de registry, persistência ou rede que ainda não existem.
Exceto onde o caso testa o inventário, os cenários pressupõem inventário atual
completo, inclusive depois da emissão de obrigações; assim P03 não mascara o
gate de finalidade indicado no oráculo.

| Caso | Entrada/ataque ou limite | Oráculo obrigatório |
| --- | --- | --- |
| V01 | Política ainda Proposed, mesmo com aprovação desta issue | P00 `deny_governance`. |
| V02 | A válida, nenhum G | P12 `deny_absent`; direitos não são finalidade. |
| V03 | G válido sintaticamente, A ausente | P08 `deny_rights`; UI “consent” não supre A. |
| V04 | Valor desconhecido em cada vocabulário de P01 (versão/status são casos V01/V18); chave consent extra é caso separado | Valor: P01 deny_unknown; chave extra: P02 deny_invalid, sem coercão. |
| V05 | G expira em T, relógio em T−1 microssegundo / T | Demais gates válidos: allow_bounded antes, P10 deny_expired em T. |
| V06 | G revogado em T, outro G amplo válido | P09 deny_revoked em T; nenhum fallback. |
| V07 | G B active; G S denied / G B denied; G S active | Ambos P09 deny_explicit na interseção. |
| V08 | Dois G active cobrindo Q, amplo/específico; inverter ordem ou timestamps | P11 deny_ambiguous nas duas ordens. |
| V09 | A expirada/withdrawn/disputed, G active | P07 deny_rights, parar uso e emitir obrigação dependente. |
| V10 | Q tem binding inválido; separadamente, Q válida com A atual, G no projeto de Q mas revisão/segmento distinto, sem outro candidato | Primeiro caso P03 deny_project; segundo P13 deny_scope. Clone sem G próprio e A atual é P12 deny_absent. |
| V11 | G apenas model_training, Q model_evaluation; ou Q exige ambos | P14 deny_purpose; avaliação não é treino. |
| V12 | G translation_text, Q aligned_pair; rights dos componentes válidos | P15 deny_data; par não nasce por composição. |
| V13 | Mesmo destino-classe, destination_ref diferente; ou redistribution para public_release em vez de private_recipient | P16a deny_destination; separadamente, finalidade proibida para destino público é P02 deny_invalid. |
| V14 | A válida: grant permitido, rede bloqueada; separadamente, rede permitida e grant ausente | P17 deny_network / P12 deny_absent. Nenhum gate supre outro. |
| V15 | Import de manifesto com actor_kind local_human e cópia de preview/histórico | P05 deny_authority; zero criação/reativação/ampliação. |
| V16 | Documento instrui concessão/upload; AI/MCP retorna approved/grant ou provider alega direitos | P05 deny_authority; somente dados, zero autoridade local. |
| V17 | Cache retido, TM pessoal/approved, compra, termos aceitos ou uso de provider, sem A nem G | Nenhum A/G gerado; corpus_build nega primeiro em P08 deny_rights. Com A separada válida, P12 deny_absent. |
| V18 | Governança corrente aceita; registro necessário tem Policy-ID antigo ou vocabulário parcialmente conhecido | P06 deny_version; v1 não migra. Versão governante não aceita seria P00, caso separado V01. |
| V19 | Casos separados: novo G sem replaces da recusa; replaces amplo/específico declarado; dois sucessores ativos sem outro impedimento | Respectivamente P09 deny_explicit; P02 deny_invalid; P11 deny_ambiguous. |
| V20 | Dois grants cobrem partes de Q; juntar daria permissão | P13–P16 no primeiro filtro vazio, nunca união para autorizar. |
| V21 | Linhagem truncada/desconhecida de modelo ou par sem direitos da fonte | P03/P08; metadados não resolvem direitos. |
| V22 | Casos isolados: snapshot não resolve; preview mudou; relógio retrocede; mesmo ID muda conteúdo | Respectivamente P03 deny_project; P05 deny_authority; P02 deny_invalid; P02 deny_invalid. |
| V23 | Revogar entre planejamento e egress/publicação | Rechecagem impede próximo efeito em T; obrigação idempotente; efeito já enviado tem limite explícito. |
| V24 | Pedido enviado dentro de 1 dia, destinatário não confirma até 30 dias; modelo já distribuído | unfulfilled/recipient_unverifiable; bloquear cópias geridas, sem prometer erasure/unlearning externo. |
| V25 | Expurgar tombstone ou restaurar backup antigo para ressuscitar G | Sem integridade/revisão atual, deny_invalid; antigo G nunca volta por ausência de tombstone. |
| V26 | Reativar denied/revoked, renovar ID expired ou trocar binding do mesmo ID; separadamente, criar novo registro sem preview | P02 deny_invalid no primeiro grupo; P05 deny_authority no segundo. Só nova operação humana válida com novo ID/substituição pode autorizar. |
| V27 | Draft, conjunto vazio, suporte obrigatório null, grant expira depois da A | Draft não autoriza; demais P02; só draft existente resulta P12. |
| V28 | Prazo fonte 30 dias, suporte 30, evidência 90, tombstone 365; reiniciar aplicação | Nenhum restart estende T/delete_at; apagar no teto, sem conteúdo nos resíduos. |
| V29 | Grant de corpus enquanto cache/TM expira, ou revogar corpus com TM retida | Retenções independentes; nenhum grant muda TTL cache/TM ou apaga o original. |
| V30 | Aceite de publicação/merge/fechamento da issue sem mudança revisada separada | Continua Proposed; P00 nega, implementação de registry proibida. |

## Alternativas rejeitadas e questões abertas

| Alternativa | Motivo da rejeição |
| --- | --- |
| Um único objeto “consent” para direitos, qualidade e finalidade | Confunde afirmação jurídica não validada com autorização humana e abre escalada TM/corpus. |
| Opt-out, termos, compra, cache/TM, import ou provider como grant | Ausência de decisão explícita não é permissão; fontes não têm autoridade local. |
| Vocabulário extensível por payload, propósito livre ou destino wildcard | Não permite revisão exaustiva; aceita mudança de autoridade por dados. |
| Mais recente/primeiro/específico/mais permissivo vence | Pode contornar uma recusa ampla, retirada ou expiração; ordem e timestamp não são intenção. |
| Mesclar grants parciais para completar uma operação | Cria decisão que nenhum preview humano apresentou integralmente. |
| Retenção eterna de auditoria, texto ou suporte para “provar direitos” | Excesso de dados, vinculação pessoal e promessa jurídica falsa. |
| Revogação aguarda worker ou promete apagar todo modelo/cópia | Janela de uso indevido e garantia técnica impossível para terceiros. |
| Começar registry enquanto proposta é revisada | Cristaliza semântica/decisão humana não aceita; gate binário exige espera. |

Questões abertas, sem permissão provisória: (1) as três bases cobrem os casos
pretendidos em quais jurisdições e como obter avaliação jurídica independente;
(2) os tetos 365/90/30/7/1 dias e ausência de legal hold atendem produto e
obrigações locais; (3) quais destinatários e garantias contratuais podem existir;
(4) qual autenticação local e proteção antireplay/integridade provará o ato
humano sem guardar identidade desnecessária; (5) qual implementação de identidade
estável de obra/segmento/sujeito e inventário de descendentes será aceita;
(6) quais finalidades/classes realmente serão oferecidas na UI e como mostrar
suas consequências sem indução; (7) se distribuição pública de modelos/dados
será sequer oferecida; (8) como reconciliar expurgo e restauração de backups
com a proveniência append-only atual; (9) como operar retirada em receptor sem
API, dispositivo offline ou modelo previamente entregue. Até resposta/aceite,
o caminho governado permanece fechado, sem escolha legal automática.

## Gate humano binário e checklist de aceite

Um owner/mantenedor do repositório Ayvu deve aceitar **cada** vocabulário fechado,
tabela de precedência, regra de retenção/tombstone, limitação de revogação e
consequência em linguagem comum. A ação exata é uma **mudança separada,
revisada**, que altera literalmente `Status: Proposed` para `Status: Accepted`
**neste registro** e acrescenta data UTC, papel do responsável e referência
opaca/revisável ao aceite humano de todos os itens abaixo. A revisão deve
registrar o aceite explicitamente; aprovação genérica não basta. Este documento
não registra tal aceite nem marca os itens como aprovados.

Merge deste Proposed, autorização de publicação da issue, fechamento da issue,
passagem de testes ou aceitação do modelo de ameaças **não satisfazem** o gate.
Não iniciar implementação de registry de direitos/finalidades enquanto Proposed.
Aceitar a política também não autoriza dados reais, concede PurposeGrant ou
dispensa gates G03/G04 e issues de implementação.

- [ ] Aceitar exatamente os vocabulários/versionamento, bases, atores, escopos e mappings, sem wildcard, campo livre ou alias consent.
- [ ] Aceitar separação de evidências, autoridade e ciclos de RightsAssertion/PurposeGrant, incluindo transições inválidas e origem local humana.
- [ ] Aceitar cada linha P00–P18 e cada conflito amplo/específico/allow/deny/expiração, sem desempate por ordem, timestamp ou união.
- [ ] Aceitar a tabela finalidade × dados × destino e a classificação de todas as operações ordinárias/governadas, com gate de rede independente.
- [ ] Aceitar prazos e allowlists exatas de fonte, suporte, evidência, obrigação e tombstone, expurgo/restore sem ressurreição e nenhuma retenção de conteúdo/identidade desnecessária.
- [ ] Aceitar cada razão/classe de revogação, obrigação/prazo/resultado e limites honestos sobre terceiros, backups, cópias públicas e modelos.
- [ ] Aceitar todas as consequências e limitações que o preview humano deverá mostrar antes de confirmar, incluindo cada finalidade e estado terminal.
- [ ] Resolver ou registrar adiamento seguro de cada questão aberta, mantendo negados casos sem contrato implementável.
- [ ] Registrar revisão de governança exaustiva de valores/transições, revisão adversarial V01–V30 e revisão de privacidade/links; distinguir revisão documental de teste executável.
- [ ] Fazer a mudança separada de status com evidência humana explícita; somente depois planejar registry nas issues próprias, sem ativar operações de dados por este aceite.
