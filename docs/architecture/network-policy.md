# Política HTTP por execução (#159)

O vocabulário fechado é `none` e `loopback`. `network_policy.resolve_network_policy`
resolve a seleção na composição, sem I/O, e produz `NetworkDecision` imutável.
Seleção ausente registra `defaulted=True` e usa `loopback`; valores desconhecidos
falham fechados. Nenhuma seleção é persistida em preferências, projetos ou checkpoints.

`cli_composition` (#158) encaminha a entrada ao preflight e a
`provider_composition`. O preflight cria uma única `NetworkAuthorization`,
compartilhada pela fábrica de workers; cada worker mantém sua sessão HTTP.
`CliProviderSession` deriva desse mesmo snapshot as decisões vinculadas exigidas
pelos casos de uso de #128. Descoberta e diagnóstico continuam usando seus DTOs,
operações e contratos existentes. A autorização final também está no tradutor,
inclusive quando consumido diretamente, sem CLI. Nenhum contrato importa Typer,
Rich ou Desktop; não há nova entrada Desktop.

## Destinos aceitos e recusados

Somente `http`. Os hosts aceitos são `localhost` e `localhost.` (sem distinção de
maiúsculas), IPv4 decimal canônico em `127.0.0.0/8`, e IPv6 `::1`, entre colchetes
na URL (também se aceita sua escrita completa, normalizada para `::1`).
Portas são decimais de 1 a 65535; omitida significa 80. Prefixos de caminho e a
URL legada terminada em `/translate` continuam aceitos.

São recusados antes de resolução: outros nomes, subdomínios de localhost,
IPv4 abreviado/octal/hexadecimal/inteiro, IPv4 mapeado em IPv6, zonas IPv6,
userinfo, consulta, fragmento, controles, espaços, barras invertidas, authorities
ambíguas e esquemas diferentes de HTTP. LAN, VPN, RFC1918/ULA, link-local,
metadata, multicast, unspecified, broadcast e público não recebem autoridade
por serem alcançáveis ou aparentemente privados. HTTPS exige outro contrato de
certificado/identidade; destinos remotos exigem uma futura política/allowlist
explícita. Não há migração silenciosa de URLs existentes.

`NetworkAuthorization` resolve um nome localhost uma vez por execução usando um
resolver injetável. Todos os resultados precisam ter família/tipo/protocolo,
porta e endereço loopback válidos, sem zona/scope/flow. Lista vazia, resposta
mista ou inválida é recusada. O endereço literal dispensa DNS. Falha de resolução
é falha de conexão, distinta de recusa de política, e não produz outra resolução.

`network_http.PinnedLoopbackAdapter` usa a implementação Requests/urllib3 existente,
com `HTTPConnection._new_conn` conectando diretamente aos sockaddrs validados.
Se o primeiro endereço não aceitar conexão, somente os demais endereços da mesma
observação podem ser tentados. Nem retries nem workers entregam o nome a uma
segunda resolução. O nome canônico permanece no Host HTTP, sem alterar o destino
do socket. O transporte só aceita os dois endpoints autorizados.

## Garantias e diagnósticos

`none` recusa antes de resolver, criar socket/sessão, despachar por Requests,
processar redirects ou construir/serializar o payload de tradução. Cache-only e
dry-run mantêm seus fluxos sem HTTP; não são novos mecanismos de isolamento.

Todas as sessões reais usam `trust_env=False`; proxies de ambiente, `.netrc` e
credenciais ambientes não participam do envio. Proxy e credenciais explícitos
continuam sem contrato e não são entradas suportadas. GET e POST usam
`allow_redirects=False`; qualquer 3xx é `provider_redirect_refused`, sem segundo
pedido, mesmo para o mesmo host. Continuam existindo somente GET `/languages` e
POST `/translate`, com os controles de timeout, retry e rate limit existentes.

Recusas expõem operação, política, classe, motivo e origem padrão/explícita,
sem URL, texto do livro, resposta, credencial ou material de `.netrc`. A CLI
apresenta a ação necessária também no modo comum; detalhes limitados ficam no
modo desenvolvedor. Erros de conexão e protocolo mantêm categorias próprias.

## Leitor externo

O antigo `open_library_epub()` de `library.py` já foi substituído na main por
`LibraryService.open_entry` e `ReaderLauncher.launch`. Esse limite continua sendo
um lançamento de processo independente. A CLI avisa antes do lançamento que o
leitor fica fora da garantia offline, inclusive com `none`. O resultado de abrir
um livro não é um recibo de isolamento HTTP, e a política não contém subprocessos,
o servidor LibreTranslate ou seus próprios destinatários.

## Evidência automatizada

`test_network_policy.py` instrumenta DNS, sockets, Session/adapter send, construção
do payload e preparação do corpo; simula o socket externo, mantendo Requests,
urllib3, framing HTTP e parsing reais. Cobre classificação, zona, respostas mistas,
rebinding, proxies, `.netrc` e redirects. `test_network_policy_flows.py` percorre
composição, #128, preflight, workers, CLI, menus, preview, tradução, lote, retomada
e leitor externo. Nenhum teste usa servidor ou dados pessoais reais. Integrações
que exigem um filesystem confiável devem ser executadas fora de sandboxes que
rejeitem os ancestrais temporários com `unsafe_path`.
