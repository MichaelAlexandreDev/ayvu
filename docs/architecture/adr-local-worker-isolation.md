# ADR: ciclo de vida e IPC de workers locais confiáveis — DESKTOP-136

Status: `Proposed`

Data: 2026-11-06. Issue: [#167](https://github.com/DevStarrk1137/ayvu/issues/167).
Não há aceite humano desta decisão nem autorização para habilitar um runner.

## Gate e comportamento preservado

O gate foi verificado no `main` local, HEAD `3e667fb`, sem rede. Os cinco
artefatos estão versionados nesse commit:

| Dependência | Artefato e fato necessário |
| --- | --- |
| #124 | [Stack Desktop](adr-desktop-stack.md#acceptance-record): `Accepted`, aceite do mantenedor em 2026-11-05; PySide6/Qt Widgets, Linux x86_64 como primeiro alvo de avaliação/produto; Windows/macOS adiados até evidência nativa |
| #126 | [Eventos e cancelamento](job-events-cancellation.md): schema 1, produtor único, outcome, falhas e safe points |
| #129 | [Threat model](product-threat-model.md): `Accepted`, aceite em 2026-11-05; subprocesso não é sandbox |
| #137 | [Artefatos](job-artifacts.md): `Accepted`, staging/ready/abandoned, lease e reconciliação |
| #159 | [Política HTTP](network-policy.md): `none`/`loopback`, negação antes de I/O e sem autoridade ambiente |

A #124 aceita o toolkit, mantendo gates de distribuição/acessibilidade. Não
existe suporte Desktop entregue. A interpretação conservadora para esta matriz
é medir Linux x86_64, sem promover Windows/macOS a alvos aceitos. Trechos antigos
da #124 ainda dizem que o aceite está pendente; prevalece seu registro explícito.
Esta pesquisa não modifica plataformas nem resolve os gates de release.

O paralelismo de `epub_io.py` com `ThreadPoolExecutor`, callbacks da CLI e retomada
v1 continua intacto. A `main` já tem contratos de jobs e artefatos posteriores ao
contexto original da issue; sua existência não é um supervisor de processos nem
prova de contenção. A CLI `--help` foi executada em home/config/cwd temporários:
código 0, 3973 bytes de stdout e nenhum stderr.

## Recomendação reversível e confiança inicial

Propor `subprocess.Popen` com entrada fixa do pacote e um `AF_UNIX socketpair`
anônimo herdado para IPC, um worker por execução e grupo de processos próprio.
Os dois canais stdout/stderr ficam separados do protocolo. A escolha prioriza
essa separação e a ausência de endpoint nomeado; os tempos próximos não provam
superioridade de desempenho. Não reutilizar processos entre jobs na v1.

**Somente comandos in-tree revisados e allowlisted são confiáveis.** O chamador
seleciona um identificador e um schema fechado; não fornece executável, módulo,
código, argv livre ou shell. Plugins, dependências de origem arbitrária, modelos
executáveis e comandos externos exigem outra decisão. Um parser comprometido
continua com a autoridade do usuário. Esta recomendação é separação de processos,
sem promessa de sandbox de segurança.

O lançamento mínimo é obrigatório nas duas alternativas: executável resolvido
pela instalação confiável, entrada fixa, `shell=False`, ambiente substituído por
allowlist mínima, cwd temporário exclusivo `0700`, `close_fds=True`, apenas IPC e
streams exigidos herdados, limites de tempo e cleanup do grupo. No spike, Python
usa `-I -S`, e o ambiente contém apenas `LANG=C` e `LC_ALL=C`; nem PATH, HOME,
PYTHONPATH, proxies ou variáveis do pai são copiados. Um pacote real precisará
validar seu bootstrap instalado; `-S` não é uma receita para importar Ayvu.
Qualquer mecanismo que não cumpra esse mínimo fica recusado para produção.

Não existe campo de credencial ou resolução de `SecretRef`. Nem credenciais em
argv, ambiente, arquivos temporários, mensagens ou diagnósticos são autorizadas.
Um adaptador real de referências de segredo, aceito e implementado, é gate para
qualquer capacidade futura que precise disso; a decisão proposta da #160 não o
satisfaz. Canários desta pesquisa são aleatórios e sintéticos, sem credenciais.

## Comparação sob os mesmos critérios

| Critério | Popen + stdin/stdout com JSON framed | Popen + socketpair com JSON framed (proposto) |
| --- | --- | --- |
| Startup fresco, 7 amostras | Mediana 24,910 ms | Mediana 27,204 ms |
| Packaging | stdlib; reexec da entrada fixa precisa de smoke no bundle | stdlib; mesmo gate de bundle, mais passagem explícita do fd |
| Portabilidade | Pipes existem nos três OS, mas este loop POSIX com select só foi executado em Linux | AF_UNIX herdado medido somente em Linux; nenhuma promessa Windows/macOS |
| Framing | Prefixo uint32 big-endian + JSON canônico | Mesmo framing e codec |
| Backpressure | Pipe nativo bloqueia produtor; fila da aplicação com crédito | Socket nativo bloqueia produtor; mesma fila com crédito |
| Stdout acidental | Corrompe o protocolo; deve falhar fechado | Drenado/descartado fora do IPC; limite independente de stderr |
| Cancelamento | Mensagem por stdin; sem interrupção mágica de chamada bloqueada | Mensagem pelo socket; mesma limitação |
| Crash/EOF | EOF ou deadline; código de saída necessário, insuficiente para sucesso | Idem; stdout não oculta EOF do IPC |
| Cleanup | Grupo POSIX e espera limitada | Mesmo mecanismo; fechar as duas pontas criadas pelo pai |
| Pai morreu | Leitor de EOF cooperativo mata o próprio grupo | Idem; escritor do pai não pode vazar para descendentes |
| Peer/endpoint | Descritores privados criados pelo SO, sem listener | Capacidade fd privada, par criado atomicamente, sem listener/endereço em disco |
| Throughput sintético, 128 frames | 15410,1 frames/s; 8,306 ms | 16773,9 frames/s; 7,631 ms |

Pipes permanecem alternativa viável e reversível se embalagem de descritores
falhar. Não há mudança silenciosa de transporte: trocar exige repetir a matriz.
`multiprocessing` com spawn também é compatível com Python, mas seus canais de
objetos usuais e filas serializam objetos; eles ficam recusados neste desenho.
Uma variante limitada a `send_bytes` com codec próprio demandaria os mesmos
controles de framing e mais um bootstrap de packaging, sem benefício demonstrado.
Não usar `Connection.recv`, `Queue`, `pickle`, `eval`, hooks de objetos, importação
de classes pelo decoder ou exec de payload. Threads não são candidato a isolamento
nem substituto para o caminho existente. Endpoint TCP local adicionaria listener,
autenticação e disputa de porta desnecessários; não foi selecionado.

## Envelope e rejeição determinística

O protocolo externo é independente do schema canônico de eventos da #126.
A forma abaixo descreve o envelope, sem IDs, conteúdo ou caminhos de usuário:

```text
uint32_be comprimento | JSON ASCII canônico
{v:1, run:UUID, job:UUID, request:UUID, seq:inteiro, kind:tipo, body:objeto fechado}
```

`run` identifica uma única tentativa; `job` é a referência opaca durável, e
`request` correlaciona comando/resposta e a operação. Todos são UUIDs canônicos,
sem significado de caminho ou autorização. `seq` é por direção, começa em 1 e
cresce exatamente de um em um, abaixo de 2^31. Não é a sequência de `JobEvent`.
Mudança de run/job/request é `invalid_peer`; lacuna/replay/regressão do transporte
é `invalid_sequence`. Rejeições não avançam sequência. Replays duráveis continuam
obedecendo ao `JobEventSequence`, inclusive duplicata idêntica, sem redefini-lo.

| Tipo | Contrato proposto |
| --- | --- |
| `hello` | Primeiro em cada direção. Pai oferece `versions:[1]`, `event_schema:1`; filho confirma `version:1`, `event_schema:1`. Sem interseção, versão desconhecida, mudança durante execução ou hello repetido: recusar, sem downgrade nem comando |
| `command` | Um por tentativa. Tipo/version allowlisted, referências opacas de job e artefatos autorizados e deadline UTC opcional do contrato da operação. Nada de caminho, texto, URL, codec de objetos ou política nova |
| `event` | Dados de progresso/aviso/ack conforme #126; sem strings livres. Supervisor valida os DTOs e permanece produtor único do stream durável, atribuindo IDs, timestamps, correlação e sequência canônica |
| `cancel` | `CancellationRequest`: reason code fechado e requested_at UTC; idempotente, encaminhado ao `CancellationSource`. Ack só no safe point declarado; pedido não significa outcome |
| `result` | Uma resposta por request: `JobOutcome` candidato segundo #126 e até 16 referências UUID de artefatos já reservados; nenhum payload ou localização. Supervisor valida lease, identidade, integridade, estado e publicação antes de decidir qualquer sucesso |

No spike não existe job runner: `event.body.event` carrega fixtures canônicas da
#126 e `result` usa `artifacts:[]`. O teste decodifica as fixtures com o decoder e
ordenador reais, inclusive request/ack/outcome cancelado. A implementação futura
terá o supervisor como produtor único; não copiar o produtor sintético do spike.
Os campos numéricos `pids` e flags de autoridade são extensões **exclusivas de
probe**, não campos de produção nem diagnósticos persistíveis.

A leitura verifica o prefixo antes de reservar corpo. JSON padrão, sem hooks,
passa por limite lexical de profundidade antes do parser e re-encoding canônico
com igualdade byte a byte. Isso recusa duplicatas, whitespace alternativo, UTF-8
não canônico, campos extras/ausentes, NaN/Infinity e formas executáveis. UUIDs,
tipos exatos (bool não é inteiro), schemas e direção são validados antes de usar
um payload. O validador de produção reutilizará os contratos existentes para o
corpo; não substituirá `decode_event` nem suas regras de ordenação.

| Orçamento determinístico da pesquisa/proposta inicial | Limite |
| --- | --- |
| Corpo de frame / cabeçalho | 32768 bytes / 4 bytes |
| Evento canônico interno | 16384 bytes, conforme #126 |
| Profundidade JSON | 8 níveis antes do parse |
| Fila de aplicação | 8 frames (262144 bytes brutos), mais um frame em leitura e um em escrita; objetos Python têm overhead adicional |
| Jobs/comandos simultâneos por canal | 1; controle de cancelamento precisa de leitor independente do trabalho |
| I/O de frame | 1 s total por frame, sem renovar em cada fragmento |
| Startup | até 1 s para envio de hello + 3 s para resposta; encerrar se não completar |
| Deadline de operação | UTC recebido pela operação, convertido uma vez para relógio monotônico; clamp ao orçamento local; nunca estendido por progresso/retry |
| Cancel grace / TERM / KILL | 250 ms / 250 ms / 1000 ms; no máximo 1500 ms após pedido de cancelamento antes de declarar cleanup pendente |
| Fechamento da sessão | até 1250 ms de término + duas esperas de dreno de 1000 ms (3250 ms); limite sob escalonamento normal, sem garantia de tempo real |
| Stdout e stderr | 16384 bytes cada como quota cumulativa; descartar os bytes, contador saturado em 16385; ultrapassar recusa sucesso |
| Buffer por dreno | 4096 bytes; continua descartando até encerramento, sem acumular saída |
| Caso de teste / medição completa | 8 s por teste (SIGALRM); 120 s no coletor e comando externo de teste |

A fila cheia impede nova leitura até liberar crédito; nenhum evento aceito é
silenciosamente perdido. Produtor parado e deadline expirado levam a falha e
cleanup, não a fila ilimitada. O leitor de cancelamento precisa continuar mesmo
com a fila de eventos cheia; o spike mede bloqueio real do transporte, mas não
implementa o multiplexador do supervisor. Buffers nativos têm capacidade finita
administrada pelo SO; não são o orçamento de heap da aplicação. O sandbox recusou
`getsockopt` e `setsockopt`, portanto **nenhum teto numérico de buffer nativo foi
medido ou imposto aqui**. Um gate do adaptador futuro é configurar/verificar esse
teto no host empacotado, ou rejeitar a configuração se o produto o exigir.

EOF/truncamento/crash é `worker_crashed`; frame inválido é `invalid_frame`, prefixo
acima do teto é `oversized_frame`, versão é `incompatible_version`, schema/tipo é
`invalid_message`, prazo é `timeout`. A primeira falha inutiliza a sessão: nem
resposta tardia nem exit 0 a recuperam. Encerrar grupo, descartar resultados em
trânsito e fechar descritores. Erros são códigos de allowlist, sem corpo bruto,
stdout/stderr, caminho, canário, valor de ambiente ou mensagem de exceção original.

## Vinculação do peer, filesystem e política de rede

O SO cria as duas pontas do `socketpair` atomicamente. O pai passa exclusivamente
a ponta filha usando `pass_fds`, fecha sua cópia e guarda a outra; o filho marca o
fd não herdável em exec. A própria posse do descritor é a autoridade por execução;
não há token secreto em argv, ambiente ou handshake. IDs adicionais vinculam a
conversa, mas não autenticam alguém que já roubou o descritor. Não usar PID do
`SO_PEERCRED` de um par pré-criado como prova de identidade do worker.

Um processo separado sem fd herdado tentou injetar e recebeu recusa do SO. O
worker também enviou run incorreto pelo canal real: o receptor recusou
`invalid_peer`. Um arquivo sintético com nome de socket antigo permaneceu intacto.
Sem bind/listen/pathname não há endpoint obsoleto para reaproveitar, substituir
ou unlinkar; cleanup fecha descritores, nunca remove arquivos de terceiros.
Processo do mesmo usuário capaz de inspecionar/subverter o pai continua fora da
garantia. Fork herda descritores até exec; o worker confiável precisa fechar os
não exigidos ao criar descendentes. Esse é um risco operacional, não isolamento.

Cwd não limita filesystem: o worker leu e escreveu em um diretório irmão com
canários sintéticos. Acesso às demais permissões do usuário não foi examinado.
A política `NetworkDecision` da #159 continua `none`/`loopback`: deve ser resolvida
no pai e revalidada no adaptador autorizado do worker, sem rede implícita nem
credenciais ambiente. Não serializar o objeto Python; apenas a representação de
dados do contrato aceito, vinculada à execução e à operação. Não introduzir
classes de rede concorrentes ou inferir autorização a partir da alcançabilidade.

O teste de listener controlado ficou **skipped**: o sandbox recusa criar socket
AF_INET. Não se testou Internet, DNS, LAN nem listener real do usuário. A recusa do
host não prova negação causada pelo mecanismo selecionado. Como nenhuma política
OS de rede faz parte desta baseline, filesystem e network containment estão
indisponíveis. `none` só é exigível no código confiável via #159; se o requisito for
negação OS mesmo após comprometimento do worker, rejeitar esta proposta e abrir
revisão separada de isolamento antes de executar a operação. Não contornar #159.

## Matriz de controles por plataforma

E = imposto pelo mecanismo explicitamente indicado; B = best-effort sob código
confiável e escalonamento normal; U = indisponível na baseline. NM = não medido,
sem atribuição de garantia. Não existe controle de GPU nesta pesquisa.

| Controle | Linux x86_64, medido | Windows, adiado por #124 | macOS, adiado por #124 |
| --- | --- | --- | --- |
| Ambiente mínimo / fds não exigidos | E no lançamento Popen; ausência de canário e fd verificada | NM; não portar pass_fds/select como se fossem handles Windows | NM |
| Cwd temporário explícito | E como diretório inicial; não contém filesystem | NM | NM |
| Filesystem | U; leitura/escrita fora do cwd em raízes sintéticas confirmadas | U/NM | U/NM |
| Rede | U; probe loopback skipped pelo host; #159 é controle da aplicação | U/NM | U/NM |
| Criação de filhos | U; subprocesso sintético executou | U/NM | U/NM |
| Cleanup de filhos/netos | B: killpg + espera; casos sem escape passaram | NM; avaliar mecanismo nativo em pesquisa própria | NM; grupos ainda não medidos |
| Parede / cancelamento | B: deadlines, EOF, TERM/KILL; não é prazo de tempo real | NM | NM |
| Memória | U para RSS/árvore; E experimental para espaço virtual por processo via RLIMIT_AS, 96 MiB, alocação de 128 MiB recusada | U/NM | U/NM |
| Contagem de processos | U: grupo não é quota; limite por UID não é limite por job | U/NM | U/NM |
| CPU | U para taxa/quota de árvore; E experimental de tempo CPU por processo com RLIMIT_CPU, SIGXCPU observado (soft 1 s, hard 2 s) | U/NM | U/NM |
| Recuperação após crash do pai | B: thread observa EOF e mata grupo; worker parado/comprometido pode não cooperar | NM | NM |

RLIMITs foram aplicados no próprio bootstrap sintético. Não há instalação desses
limites na produção nem prova de RSS, CPU percentual ou total de descendentes.
Não usar preexec_fn no processo multithread de UI. Uma futura entrada confiável
poderia baixar limites hard antes de carregar trabalho, após validação específica.
Cgroups/delegação, namespaces, seccomp, mecanismos nativos de Windows/macOS e
empacotamento não foram sondados nem aceitos. Para garantir quota de árvore,
cleanup após morte simultânea de pai e worker, ou conter descendants que usam
setsid, será necessária outra primitiva e nova evidência. Não alterar a matriz de
plataformas para esconder essa lacuna; recomendar revisão de escopo se exigida.

## Cancelamento, morte do pai e reconciliação

O pedido cooperativo é idempotente; só o safe point confirma o ack. A escalada
mata o grupo inclusive depois de o líder sair. Morte por sinal não significa
`cancelled` reconhecido nem `validated`. O supervisor futuro aplica as transições
da [máquina de jobs](job-lifecycle.md) por CAS. Sem ack, falha/timeout pode virar
`JobFailure` resource/internal conforme causa; depois de ack, nunca emitir outcome
contraditório. Se faltam fatos para concluir, manter `requires_reconciliation`
sem fabricar outcome. Expiração de lease não é sucesso nem outcome.

Após EOF por morte abrupta do pai, o worker sintético com leitor separado mata
seu grupo, incluindo filho/neto. O harness Linux usa subreaper e pidfds para
observar morte e reap dos órfãos, sem confundir zumbi com processo executando.
O subreaper pertence só ao teste, não é garantia de produção. SIGSTOP, escape de
grupo, kernel bloqueado e morte do leitor não estão contidos; EOF sozinho é
best-effort. Crash do worker com descendentes vivos exige deadline e killpg do
pai. Se não provar saída no orçamento, registrar cleanup pendente e bloquear
reutilização/publicação; não apagar conteúdo com base apenas no PID.

A resposta do worker nunca publica artefato. Somente o serviço #137 pode reservar,
verificar tamanho/digest, publicar sem overwrite e fazer CAS para ready sob lease
válida. Um arquivo `.part` permanece staging/incompleto após crash e novo worker.
A reconciliação real de `ArtifactService`, exercitada com ports sintéticos, manteve
lease ativa intacta e abandonou ausência de digest ou de arquivo final com os
motivos canônicos. SQLite e payload store reais continuam sob os testes de #137;
a integração deles não passou neste sandbox por `unsafe_path`. Não atribuir aos
ports sintéticos prova dessa integração. A [retomada](job-recovery.md) não muda.

## Evidência, operação e desdobramento

Fontes, comandos, limitações e matriz de testes estão no
[spike removível](../../spikes/local-worker/README.md), com
[métricas estruturadas](../../spikes/local-worker/results/linux-x86_64.json).
São observações locais em Python 3.12.12/Linux x86_64, sem reiniciar o host nem
limpar page cache, não metas de desempenho nem resultados de distribuição.
Nenhum source text, EPUB, tradução, caminho absoluto, valor de canário ou segredo
entra na evidência. Os scripts não importam Ayvu; só testes consomem contratos
existentes para verificar compatibilidade. Não há dependência/entrypoint instalado.

Propostas de follow-up, cada qual revisável e sem criar issues remotas nesta sessão:

1. **M — codec e adapter IPC**: schemas de comando/resultado, validação dos DTOs
   existentes, produtor único no supervisor, créditos com controle responsivo e
   testes de fuzz/limites, sem execução de jobs nem segredos.
2. **L — supervisor confiável Linux**: entrada fixa empacotada, ambiente/handles,
   deadlines, cancelamento, saída limitada, cleanup e diagnóstico seguro. Gate:
   smoke do bundle e definição explícita do teto de buffer nativo.
3. **M — integração de leases/artefatos/recovery**: CAS, ownership, digest,
   resultado tardio, reconciliação e testes SQLite fora deste sandbox; manter CLI.
4. **M — contenção OS separada**: decidir se subprocesso confiável basta para cada
   operação; caso contrário medir quotas de árvore e negação de rede/filesystem,
   sem alegar sandbox antes das provas. Plugins continuam proibidos.
5. **M por plataforma — pesquisa nativa e packaging**: somente se #124 incorporar
   Windows/macOS; repetir toda a matriz, incluindo handles, árvore e morte do pai.
6. **S — revisão humana desta ADR**: aceitar/rejeitar/restringir operações e riscos,
   registrar autoria/data; até lá manter `Proposed` e sem habilitação em produção.

Se rejeitada, remover `spikes/local-worker`, seus testes/registro/links e esta ADR;
nenhuma migração de dados, rollback de CLI ou dependência terá sido necessária.
