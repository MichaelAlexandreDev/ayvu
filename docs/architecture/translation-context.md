# Contexto de tradução determinístico e limitado

Implementação da [#164 / DESKTOP-133](https://github.com/DevStarrk1137/ayvu/issues/164).
#103, #129, #138, #140, #141, #142, #145, #153, #157 e #163 fornecem os contratos
consumidos; #252 e #162 fornecem o runtime rico e a recuperação conservadora.
Não há avaliação de qualidade linguística ou execução de IA.

`TranslationContextBuilder` é política de aplicação pura. Não resolve provedor,
segredo, arquivo, rede ou ferramenta. Não importa frameworks, parser de formato,
SDK, filesystem ou persistência. Não interpreta instruções no texto.

## Entradas e autorização

O chamador resolve um `ContextSnapshot` com o `ResolvedProfileSnapshot` exato,
o `UnitReference` de #140, a unidade atual e um manifesto limitado de revisões.
Cada `ContextEntryRevision` contém projeto, referência opaca, revisão imutável,
identidade de conteúdo verificável pelo dono, tipo, finalidade, obrigatoriedade
e posição estrutural. A categoria de dados deriva exclusivamente do tipo fechado.

`ContextAuthorization` é autoridade já resolvida pelo chamador. Seu binding inclui
projeto, operação, finalidade `translation`, tipos/categorias permitidos, política,
referência exata do snapshot resolvido do perfil, documento/revisão/unidade e
intervalo UTC de emissão/expiração. Ausência, emissão futura, expiração, dados
malformados ou divergência recusam antes da primeira porta, da construção do
payload, do cache e do provedor. Vale `issued_at <= now < expires_at`.
Renovação exige nova preparação. Não se infere ou expande autoridade.

`snapshot_context_sources` compõe apenas seleções explícitas dos donos:

| Tipo | Conteúdo mínimo | Restrição |
| --- | --- | --- |
| `profile` | idioma de destino efetivo | opção explícita; estilo continua inerte |
| `current_unit` | texto visível da unidade atual | revisão #140 exata; opção explícita |
| `terminology` | termo e tradução gerenciados | binding #157/#163 exato, estágio provider e ocorrência literal na unidade |
| `protected_term` | termo gerenciado preservado | mesmas restrições; `required` gerenciado é preservado |
| `neighbor` | texto de unidade explicitamente indicada | relação estrutural #140, mesmo documento/projeto |

Não há descoberta automática de vizinhos, arquivos ou termos não relacionados.
Glossário pós-cache não vira contexto de provedor. Notas de projeto, obra e capítulo
permanecem excluídas, sem dono/repositório versionado neste contrato. Referências
não concedem autoridade para abrir caminhos. Os donos fornecem snapshots autênticos
e revisões imutáveis; texto alterado contra a identidade esperada é `context_integrity`.

## Porta e limites v1

Há uma única porta, `ContextCandidateSource.read(ContextSourceRequest)`. Ela recebe
somente snapshot validado, conjunto exato de tipos solicitado e `ContextSourceLimits`.
Não aplica política, ranking ou seleção de provedor. Pedidos acima dos tetos falham
antes de chamar a porta. Não há outro repositório de candidatos.

| Limite de execução | Teto `bounded_context_v1` |
| --- | --- |
| entradas selecionadas | 64 |
| conteúdo UTF-8 por entrada | 4.096 bytes |
| conteúdo UTF-8 efetivamente representado | 32.768 bytes, incluindo LF de separação |
| unidades vizinhas | 8 |

O manifesto admite uma sentinela de metadados N+1 (65), sem ampliar a coleção de
conteúdo. Mais de 65 revisões falham. O pedido à porta tem no máximo 64 entradas,
4.096 bytes por entrada e 32.768 bytes retornados; pode apenas diminuir esses
valores. O teto de retorno não conta separadores adicionados depois. A seleção
verifica novamente o teto da representação com os separadores.

`SnapshotContextCandidateSource` é o adapter de produção sobre revisões já
resolvidas. `StoredContextRevision` guarda comprimento antes de `load(cap)`.
Contagem, tamanho individual e soma são verificados **antes de qualquer load**.
Excesso devolve `ContextSourceOversize` sem conteúdo para o lote inteiro:
contexto obrigatório falha; consultivo recebe omissões `limit`. Não há carga
parcial, resumo, corte de texto ou fallback para capítulo/livro. Um dono com
armazenamento lazy deve respeitar o cap no carregamento; o adapter atual usa
strings imutáveis que #140/#157 já possuem. Comprimento e identidade são
conferidos novamente no conteúdo devolvido. Duplicatas, revisão divergente,
expansão de tipo/projeto e conteúdo trocado sob uma revisão são recusados.

## Ordenação e capacidades

A precedência fechada v1 é perfil, unidade atual, terminologia, termo protegido,
vizinho. Desempates: tupla de posição, referência opaca e revisão, usando a ordem
de inteiros e strings ASCII opacas. Locale e ordem de inserção não participam.
Nenhuma decisão usa nome, URL ou tipo de adapter de um provedor.

Entradas são incluídas inteiras nessa ordem. Excesso individual/agregado/contagem,
limite de vizinhos ou candidato ausente gera omissão consultiva com `limit`,
`neighbor_limit` ou `missing`. Uma entrada obrigatória nessa situação gera falha
tipada. Não se preenche espaço com outros candidatos. Avisos são deduplicados
na ordem fixa `missing`, `limit`, `unsupported`, `neighbor_limit`. Alterar teto,
precedência, desempate, omissão ou representação exige outra versão de política.

`ContextCapabilityEvidence` exige snapshot #103 para a configuração exata e
validade UTC. `unsupported` não carrega candidatos e produz omissões
`unsupported`; com entrada obrigatória, falha. `unknown`, `configuration_dependent`,
evidência vencida/futura ou limite desconhecido resultam em
`context_preflight_required`. O chamador executa o preflight limitado de #103
e reenvia a evidência; o builder nunca faz esse I/O ou presume suporte.
Evidência contraditória/malformada falha fechada.

O orçamento efetivo é o mínimo do teto local e dos limites conhecidos. #103
atualmente declara `context_characters`, não bytes. A v1 reduz conservadoramente
também os tetos em bytes ao teto de caracteres e confere ambos, sem presumir
ASCII. Um provedor explicitamente sem contexto recebe texto/locales normais,
sem `ContextFeature`. Isso mantém o corpo HTTP legado do LibreTranslate e
encaminha `context_unsupported` pelo canal de avisos existente da interface.

## Payload, cache e proveniência

`joined_lf_v1` usa um `ContextItem` #141 de papel `translation_context`, com
entradas inteiras separadas por LF. Assim, o limite de 16 itens de #141 não
altera o teto de 64 entradas. A feature é obrigatória se alguma entrada
selecionada o for. Payload e projeção semântica são sensíveis e ficam separados
de `diagnostic()`, divulgação e metadados de proveniência.

A projeção canônica ordenada contém domínio/versão da política, limites efetivos,
versão de representação, referências/revisões/identidades de conteúdo, tipos,
obrigatoriedade e posições **somente do que foi selecionado**. Não contém texto,
caminho, display name, segredo ou rótulo de UI. `OpaqueSemanticRevision` leva
esse efeito ao plano #153 de cada unidade antes de lookup/escrita raw. Conteúdo,
revisão, ordem, política, orçamento ou representação diferentes invalidam o raw.
Candidatos omitidos e estilo/glossário downstream não entram nessa identidade.
Sem feature, o plano raw continua o anterior.

`ContextExecution` valida todas as autorizações antes de carregar o primeiro
candidato, prepara o conjunto de forma atômica e vincula a preparação aos
pedidos exatos. `DocumentTranslator` confere todas as unidades contra o documento
e o plano antes de qualquer cache, inclusive resolução de pin. Confere novamente
antes de enviar chunks. A ordem raw/cache/glossário existente é preservada.
Replay de artefato bruto fica desativado com contexto para não pular revalidação.
Cache-only continua local, sem consultar gate ou provedor.

#138 registra `build_translation_context` após o artefato de resultados ficar
pronto, ligado à unidade e ao artefato. A extensão opcional guarda apenas
política, tipos/revisões opacas selecionadas, tipos/motivos omitidos e bytes.
Contagens vêm das coleções limitadas. Não guarda conteúdo nem projeção/fingerprint
de contexto. Registros antigos omitem inteiramente o campo opcional. Repr,
avisos, falhas normalizadas e diagnósticos usam allowlists de códigos/metadados.

## Saída de dados e composição

`ContextEgressDisclosure` contém destino opaco resolvido pelo chamador
(provedor/configuração/destino/revisão da política), operação do projeto,
operação de provedor `translate`, finalidade, categorias, contagem e bytes da
representação real de contexto. Não contém URL ou texto. Os bytes do texto
principal continuam sob o contrato de transporte existente; a divulgação mede
o contexto adicional, inclusive separadores.

`ContextEgressGate` devolve uma decisão já resolvida de #159, vinculada ao hash
da divulgação. `require_context_egress` consome `ResolvedProviderNetworkDecision`,
exige emissor confiável, mesma revisão, binding exato e intervalo válido, e
recusa `strict_offline`/`unclassified`. Não avalia política nem concede consentimento.
Negação, ausência, expiração, emissão futura ou divergência impedem a chamada
de provedor. O gate é revalidado imediatamente antes de cada chamada de chunk.
A política HTTP de #159 continua obrigatória no transporte; a divulgação não
autoriza URL, redirect, fallback ou outro destino. O chamador vincula o destino
opaco ao provedor/configuração já resolvido. Conteúdo não participa da resolução.

`ProjectTranslation.translation_context` injeta o conjunto autorizado na
composição rica, selecionada pelo rótulo de modelo. A composição prepara/autoriza
antes de abrir repositórios ou resolver provider; o runtime revalida antes de
preparar cache/provider. A revisão efetiva entra em
`TranslateProjectCommand.context_revision` e no `resolve_plan` do job apenas
quando declarada. Sem contexto/rótulo, o legado mantém assinaturas, mensagens,
fingerprint e bytes dos checkpoints. Não há flag/configuração nova para conceder
autorização implicitamente. Um consumidor de aplicação resolve snapshots,
autorização, capacidades, destino e gate, constrói `ContextExecution` e o passa
à composição. Informa também `operation_id` igual ao da autorização,
`model_label` válido e `cache_path` explícito, conforme o contrato do runtime
rico. Um perfil nomeado sozinho não ativa envio de contexto.

Cancelamento é checado antes da carga, entre fases/entradas limitadas, antes do
payload e antes da divulgação. A composição observa também o cancelamento
vinculado do batch durante preparação. Falha/cancelamento não deixa conjunto
parcialmente executável, artefato de contexto ou cache lookup.

## Fronteira com futuras funções de IA

Documentos, termos e respostas do provider são dados não confiáveis. Não podem
escolher projeto, fonte, arquivo, ferramenta, destino, segredo, política ou
autorização. Não se inferem personagens, fatos, estilo, relações ou consentimento.
Uma futura função de IA terá outro contrato e autoridade explícita; não substitui
esta política determinística nem as verificações de saída de dados.
