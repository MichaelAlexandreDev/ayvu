# ADR: seleção de armazenamento nativo de credenciais (DESKTOP-129)

Status: `Proposed`

Data e evidência local: 2026-11-06 (UTC).

Issue: [#160](https://github.com/DevStarrk1137/ayvu/issues/160).
Dependência obrigatória: [modelo de ameaças #129](product-threat-model.md),
`Accepted` em 2026-11-05, presente no baseline
`79bcc6ec45d603bcab1d74da58021f1edf0657ca` de `main`.
O [ADR desktop](adr-desktop-stack.md) também está `Accepted`; ele não oferece
uma matriz de suporte de sistemas operacionais ou de distribuição.

## Decisão proposta e limite de validade

Plataformas com suporte comprovado nesta revisão: **nenhuma**.
A allowlist efetiva de produção permanece vazia. Recomenda-se qualificar apenas
o candidato Linux abaixo antes de propor sua admissão. O sandbox recusou o
socket D-Bus antes de qualquer CRUD. Depois, o revisor executou a prova fora do
sandbox, na sessão autorizada, e observou CRUD real com limpeza confirmada no
ambiente travado por `uv.lock` (ver abaixo). Isso não basta para suporte:
trancado, negado/cancelado, identidade do serviço e pacote continuam sem prova,
e não há licença para promover a instalação de um pacote a garantia de armazenamento.
Este registro e os comandos de pesquisa são uma entrega parcial da #160.

| Ambiente | Versão mínima / arquitetura / distribuição | Backend único candidato | Suporte nesta revisão |
| --- | --- | --- | --- |
| Linux desktop GNOME/Wayland, Secret Service de GNOME Keyring | Somente snapshot Arch Linux rolling observado em 2026-11-06, x86_64, CPython 3.14.9, GNOME Shell 51.1, GNOME Keyring 51.1; sem extrapolação para outra versão/distro | `keyring.backends.SecretService.Keyring`, distribuição `keyring==25.7.0`, SecretStorage 3.5.0 | Pendente: falhas nativas (trancado, negado/cancelado, coleção ausente), identidade do serviço e pacote. CRUD real, indisponibilidade e resolução travada observados pelo revisor |
| Linux KWallet ou outro provedor de Secret Service | Nenhuma versão/arquitetura aprovada | Nenhum | Não suportado |
| Linux headless, contêiner, sessão sem D-Bus, sistema mínimo | Nenhuma versão/arquitetura aprovada | Nenhum | Não suportado |
| Windows | Nenhuma versão/arquitetura/formato de pacote aprovado | Nenhum | Não suportado; sem host nativo |
| macOS | Nenhuma versão/arquitetura/identidade assinada aprovada | Nenhum | Não suportado; sem host nativo |

Não existe versão mínima Linux genérica aceita. O snapshot é uma restrição de
pesquisa, não um compromisso com Arch rolling ou requisito novo para a CLI.
O alvo empacotado ainda precisa ser identificado por formato, arquitetura,
hash do artefato, resolução e identidade de execução antes de integrar a matriz.

## Evidência reproduzível e sua procedência

O [spike isolado](../../spikes/secret-store/README.md) contém manifesto,
prova executável e comandos de repetição. A
[saída sanitizada](../../spikes/secret-store/results/sandbox-2026-11-06.json)
registra timestamps, códigos de saída, inventário instalado, licenças e hashes
de metadados. O [inventário nativo observado](../../spikes/secret-store/results/native-packages-2026-11-06.txt)
é metadado do gerenciador de pacotes, não uma auditoria completa de redistribuição.
O [fechamento transitivo instalado](../../spikes/secret-store/results/native-closure-2026-11-06.json)
registra 190 pacotes, com versões/licenças declaradas, grafos e resolução de
provedores virtuais; as 190 consultas retornaram zero. Não é o inventário dos
arquivos de um executável distribuído.

| Prova executada | Resultado observado | O que permite concluir |
| --- | --- | --- |
| `native` | `access_denied`, nenhum evento CRUD, zero arquivos no HOME/config isolados | Sandbox não alcança o serviço; não prova recusa pelo GNOME Keyring |
| `missing-bus` | `access_denied`, nenhum evento CRUD | Sandbox também impede o socket ausente; não prova o erro de socket de um host normal |
| `missing-service` | `worker_failed`, exit 127 | D-Bus privado não iniciou no sandbox; não prova serviço ausente |
| Null, fail-only, chain, inesperado, classe com nome falsificado | `backend_rejected` | Política de identidade recusa objetos reais Null/fail/chain e objetos sintéticos inesperados antes de operação |
| Override de backend e de propriedade | `override_rejected` | Mesmo o nome do candidato permitido não concede autoridade via ambiente |
| `keyrings.alt` plaintext/encrypted | `dependency_missing` | Não instalado; execução com essas classes reais continua pendente |
| Resolução opcional offline | Falhou: `cffi==2.1.1` indisponível no cache novo isolado | Nenhuma resolução/lock íntegro ou instalação reproduzível comprovada |
| Host instalado | `jaraco.context` tem versão de desenvolvimento diferente do pin | Instalação do sistema não equivale ao manifesto de pesquisa |
| Empacotamento | PyInstaller ausente; resolução offline indisponível | Nenhum backend ou dependência foi validado em pacote |

A tabela acima é a execução dentro do sandbox do agente. O revisor repetiu a
prova fora dele, em 2026-11-06, na sessão GNOME autorizada pelo dono, com
`uv lock`/`uv sync --locked --no-build` num ambiente temporário (rede só para
esse ambiente; `pyproject.toml` e `uv.lock` da raiz intactos). O
[resultado do revisor](../../spikes/secret-store/results/reviewer-native-2026-11-06.json)
registra o hash do [`uv.lock` de pesquisa](../../spikes/secret-store/uv.lock).

| Prova fora do sandbox | Resultado observado | O que permite concluir |
| --- | --- | --- |
| `native` | `crud_pass`, eventos create, read, rotate, delete, `cleanup_verified`; zero arquivos isolados | O backend candidato, com as guardas, cria, lê, troca e apaga uma entrada canário na coleção padrão destrancada e confirma a limpeza |
| `missing-bus` | `unavailable`, exit 1 | Socket ausente vira indisponibilidade, sem CRUD |
| `missing-service` | `missing_service`, exit 1 | Bus privado sem `org.freedesktop.secrets` é recusado sem ativar serviço |
| Null, fail-only, chain, inesperado, nome falsificado | `backend_rejected` | Igual ao sandbox |
| Overrides no mapa avaliado e na entrada do processo | `override_rejected` | Igual ao sandbox; o override na entrada recusa antes de lançar trabalhador |
| `keyrings.alt` plaintext/encrypted (5.0.2, ambiente separado do grupo `rejected`) | `backend_rejected` | As classes reais proibidas são recusadas por identidade |
| Resolução e instalação | `uv.lock` com 13 pacotes; os 10 pins instalados batem com o manifesto (`matches_pin=true`) | Instalação reproduzível do conjunto de pesquisa; não aprova redistribuição |

Com o Python do sistema, `missing-bus` retornou `probe_failed` porque a pasta
de trabalho tinha caminho longo: o socket falso passou do limite de 108 bytes
do `AF_UNIX`. Use `--work-root` curto. Trancado, negado/cancelado e coleção
ausente continuam **não observados**: a autorização proíbe provocá-los no
chaveiro pessoal.

Não foi lida, enumerada, exportada ou alterada nenhuma entrada pessoal.
A prova gera serviço `ayvu-test-<uuid>` e conta `canary-<uuid>`, com dois valores
aleatórios não secretos, compara-os apenas em memória e não os imprime. Só
busca atributos dessa chave exata depois de tentar criá-la. A limpeza no
`finally` apaga todas as correspondências exatas e verifica ausência sem ler
valores. Falha na limpeza impede sucesso e exige investigação; interrupção
forçada do processo ou perda do serviço não permite prometer limpeza concluída.
Esses riscos exigem supervisão durante a prova fora do sandbox.

Foi inspecionada a implementação instalada: `get_default_collection()` pode
criar coleção, e `get_preferred_collection()`/`unlock()` podem destrancar.
Por isso a pesquisa usa a classe exata de keyring com guardas temporárias:
coleção padrão preexistente, sem criação/lock/unlock/exclusão de coleções,
sem prompt e sem ativação do serviço. A chamada CRUD continua no backend real.
Uma prova bem-sucedida deve registrar `guarded_upstream=true`; ela não valida
o caminho upstream sem essas guardas, nem constitui um adaptador Ayvu.

## Fontes oficiais e atualização

Sem rede nesta sessão. A inspeção atual é dos metadados e código dos pacotes
instalados, não uma alegação sobre a última versão pública. Os links abaixo
são fontes primárias para a revisão conectada; disponibilidade, conteúdo atual
e versão mais recente **não foram verificados online**. A atualização das
fontes é um gate pendente, não satisfeita por resumos de terceiros.

| Fonte oficial | Uso obrigatório na revisão |
| --- | --- |
| [keyring: documentação](https://keyring.readthedocs.io/en/latest/) | Backends, headless, desativação, configuração, segurança e packaging |
| [keyring: metadados PyPI](https://pypi.org/pypi/keyring/25.7.0/json), [licença upstream](https://github.com/jaraco/keyring/blob/v25.7.0/LICENSE) | Confirmar versão, dependências, hashes de wheels e licença MIT |
| [SecretService upstream](https://github.com/jaraco/keyring/blob/v25.7.0/keyring/backends/SecretService.py) | Identidade exata e efeitos de criação/desbloqueio |
| [SecretStorage](https://secretstorage.readthedocs.io/en/latest/), [código/licença](https://github.com/mitya57/secretstorage) | Protocolo, sessões, erros e dependências |
| [Secret Service specification](https://specifications.freedesktop.org/secret-service-spec/latest/) | Locked, prompts, atributos, sessão e identidade D-Bus |
| [GNOME Keyring](https://gitlab.gnome.org/GNOME/gnome-keyring), [GNOME libsecret](https://gnome.pages.gitlab.gnome.org/libsecret/) | Serviço nativo e seus limites; libsecret não é dependência Python selecionada |
| [Jeepney](https://gitlab.com/takluyver/jeepney), [cryptography licensing](https://cryptography.io/en/latest/faq/#what-license-is-cryptography-under) | Dependências de transporte e criptografia |
| [cffi](https://github.com/python-cffi/cffi), [pycparser](https://github.com/eliben/pycparser) | Licenças, bibliotecas e build |
| [jaraco.classes](https://github.com/jaraco/jaraco.classes), [jaraco.context](https://github.com/jaraco/jaraco.context), [jaraco.functools](https://github.com/jaraco/jaraco.functools), [more-itertools](https://github.com/more-itertools/more-itertools) | Licenças e dependências auxiliares |
| [OpenSSL license](https://openssl-library.org/source/license/), [libffi](https://github.com/libffi/libffi), [D-Bus](https://dbus.freedesktop.org/doc/dbus-specification.html), [GLib](https://gitlab.gnome.org/GNOME/glib) | Componentes nativos e condições de distribuição |
| [Windows Credential Manager](https://learn.microsoft.com/en-us/windows/win32/secauthn/credentials-management), [Apple Keychain](https://developer.apple.com/documentation/security/keychain-services) | Avaliações futuras, sem concessão de suporte |

## Dependências e distribuição

O manifesto é exclusivo de pesquisa, para CPython 3.14 x86_64 Linux. Não altera
`pyproject.toml`/`uv.lock` da raiz nem introduz extra em produção. Os pins abaixo
são o conjunto candidato. O revisor gerou um `uv.lock` de pesquisa com hashes
de wheels e fez uma instalação limpa e travada com ele; a auditoria de licenças
e obrigações dos artefatos distribuídos continua obrigatória antes de considerar
a supply chain validada. Python 3.11 acrescentaria
`importlib_metadata`/`zipp` e `backports.tarfile`; não é alvo desta proposta.

| Pacote / pin candidato | Relação | Licença observada e obrigação a verificar na redistribuição |
| --- | --- | --- |
| keyring 25.7.0 | Direta | MIT confirmada no LICENSE instalado: preservar aviso e permissão |
| SecretStorage 3.5.0 | keyring Linux | BSD-3-Clause: preservar avisos, condições e disclaimer, inclusive documentação binária |
| jeepney 0.9.0 | keyring + SecretStorage | MIT: preservar aviso e permissão |
| cryptography 50.0.2 | SecretStorage | Apache-2.0 OR BSD-3-Clause; escolher e cumprir via aplicável, incluir licenças/avisos de terceiros e de bibliotecas embarcadas |
| cffi 2.1.1 | cryptography CPython | Metadado MIT-0; texto instalado incluído na evidência; auditar libffi separadamente |
| pycparser 3.12 | cffi | BSD-3-Clause: preservar avisos e condições |
| jaraco.classes 3.4.0 | keyring | MIT confirmada no LICENSE instalado |
| jaraco.context 6.1.2 | keyring | MIT no instalado **6.1.3.dev0+g098f39c91.d20260403**; licença/artefato do pin ainda não verificados |
| jaraco.functools 4.1.0 | keyring | MIT confirmada no LICENSE instalado |
| more-itertools 11.2.1 | jaraco.classes/functools | MIT no instalado |

Os textos e hashes instalados são evidência de origem local. Não provam hashes
dos wheels upstream. O inventário inclui LICENSE, LICENSE.APACHE e LICENSE.BSD
de cryptography, mas não fecha os notices de todos os componentes nativos
efetivamente embarcados em um wheel/pacote: a cadeia de notices permanece
incompleta. Nenhuma aprovação jurídica ou de redistribuição decorre de campos
SPDX isolados.

Componentes nativos iniciais observados: GNOME Keyring `1:51.1-1`, GLib
`2.90.1-1`, D-Bus `1.16.2-2`, dbus-broker `37-3`, OpenSSL `3.6.5-1`, libffi
`3.8.0-1`, glibc `2.44+r50+g1848099f063e-1`, Python `3.14.9-1`.
O inventário transitivo fecha os metadados das dependências obrigatórias
instaladas de `gnome-keyring`, `python-keyring`, `python-cryptography`, `python`
e `dbus-broker`, incluindo famílias LGPL, GPL e licenças permissivas. Não inclui
dependências opcionais nem demonstra que todas estão carregadas ou serão
redistribuídas. **Não fecha os textos/obrigações de todas as licenças nativas
nem o inventário de um pacote Ayvu**.

Estratégia proposta: serviço de credenciais e bus fornecidos pelo sistema;
Ayvu não os instala, empacota, configura ou destranca. SecretStorage/Jeepney
usam D-Bus sem requerer dbus-python ou libsecret neste caminho. Wheels de
cryptography podem incorporar OpenSSL e outros componentes de Rust/C; o pacote
da distro e o wheel não são evidência intercambiável. O futuro pacote precisa
inventário de arquivos/DSOs efetivamente incluídos, versões, fontes e hashes,
textos de licença/NOTICE e obrigações de fonte/relink conforme a licença
aplicável. Usar serviço do sistema não elimina obrigações dos binários que
Ayvu redistribuir. Sem esse fechamento, distribuição permanece bloqueada.

## Allowlist fechada e seleção futura

Estas são regras propostas, não código de produção:

1. Validar primeiro alvo empacotado, SO/snapshot, arquitetura, conjunto de
   dependências e integridade do artefato contra evidência aprovada. Atualmente
   nenhum alvo passa este gate. Código Python substituído ou SO comprometido
   estão na base confiável de T10; comparação de nomes não é isolamento.
2. Recusar overrides de runtime `PYTHON_KEYRING_BACKEND`, qualquer
   `PYTHON_KEYRING*`, `KEYRING_*` (inclui `KEYRING_PROPERTY_*`), caminhos de
   plugins/configuração e injeções de loader. Não usar `get_keyring()`, discovery,
   prioridade ou arquivo de configuração como autoridade. Ambiente/config nunca
   recebe o valor da credencial.
3. Após aprovação humana e evidência, importar diretamente a classe registrada
   do artefato verificado e exigir `type(active) is approved_class`. Conferir
   distribuição/versão e propriedades fixas (coleção/esquema/aplicação), não
   apenas `__module__`/`__name__`. Instanciar sem overrides. O teste de classe
   isolado não detecta monkeypatch ou dependência comprometida.
4. Verificar dono atual de `org.freedesktop.secrets`, implementação GNOME Keyring
   aprovada e coleção preexistente acessível, sem autoativação/desbloqueio.
   Protocolo Secret Service e disponibilidade reportada não identificam o
   provedor; o spike **não atesta** o dono. Falta definir e provar essa
   verificação no pacote antes de admitir o alvo.
5. Exigir operação real com canário do próprio aplicativo, sem enumerar entradas
   alheias, e limpar. Revalidar identidade/estado em novas conexões/operações;
   check inicial não resolve corrida de lock/troca do serviço.
6. Qualquer falha encerra a operação sem fallback. Nunca escolher
   `keyring.backends.null.Keyring`, backend fail-only, `keyrings.alt` (inclusive
   `PlaintextKeyring` e encrypted-file), arquivos plaintext/encrypted geridos por
   Ayvu, ambiente/config, classe inesperada, subclassificação não aprovada ou
   chain. Neste desenho toda chain é recusada; só novo ADR com identificação e
   validação de **cada** backend alcançável poderia admiti-la.

T03 exige vincular futuramente a credencial ao destino autorizado; T04 impede
valores, paths e mensagens upstream em diagnósticos; T10 exige proveniência;
T11 impede prometer apagamento seguro de backups do SO. Secret Service não
deve ser anunciado como isolamento entre aplicativos do mesmo usuário. Labels
e atributos não devem conter credenciais, títulos, URLs ou paths pessoais.

## Desktop, falhas e limites de diagnóstico

O candidato pressupõe sessão GNOME Wayland interativa, bus de sessão acessível,
serviço GNOME Keyring já ativo e coleção padrão preexistente e destrancada.
Não se aceita um serviço apenas porque é ativável. Ausência de `DISPLAY` não
prova headless nem que prompts funcionam; o endpoint e o estado nativos mandam.
Sem sessão, sem coleção, bus inacessível, instalação mínima, contêiner ou
headless: nenhum suporte. Não recomendar contêiner privilegiado.

| Resultado futuro distinto | Ação comunicável ao usuário, sem erro upstream |
| --- | --- |
| Alvo sem suporte / dependência ausente | Informar que credenciais persistidas não estão disponíveis nessa instalação; usar alvo posteriormente qualificado |
| Backend inesperado / override | Recusar; orientar remover override/configuração de keyring e reiniciar com o pacote oficial |
| Bus/serviço/coleção indisponível | Informar serviço de credenciais indisponível; verificar a sessão desktop pelas ferramentas do SO; Ayvu não provisiona serviço ou coleção |
| Coleção/item locked | Informar bloqueio; usuário decide destrancar pelas ferramentas do SO e repetir; Ayvu não destranca |
| Negado/cancelado | Informar acesso negado/cancelado; respeitar recusa, sem retry automático ou troca de backend |
| Erro inesperado / timeout / cleanup não confirmado | Interromper e informar código sanitizado; nunca declarar sucesso ou apagamento confirmado |

`access_denied` observado no sandbox é uma recusa de acesso ao socket; locked e
denied/cancelled do serviço real continuam **não observados**. Fakes exercitam
guardas e limpeza, não satisfazem essas provas. A autorização desta máquina
proíbe provocar lock/unlock, criar/apagar coleções ou alterar configuração.
Para qualificar tais estados, usar sessão/VM descartável autorizada em que o
estado já esteja preparado por seu operador, sem tocar no chaveiro pessoal.
Até lá o suporte permanece ausente; não omitir esses casos da matriz.

Para Windows/macOS, nem Python de desenvolvimento nem suporte upstream provam
isolamento do pacote Ayvu. Futura pesquisa precisa nome/versão/arquitetura do
SO, formato e identidade assinada do executável, regras de acesso do Credential
Manager/Keychain, atualizações e prompts reais, além de inventário específico.
Nenhum resultado Linux é herdado por esses sistemas.

## Gate obrigatório de CI e aceite humano

Cada alvo empacotado que se quiser suportar deve executar, sem skip:

- resolução limpa do conjunto opcional, lock/hashes, licenças Python transitivas
  e componentes nativos efetivos, com notices/source/relink quando aplicável;
- identidade do backend e do serviço no executável distribuído, incluindo
  módulos/metadados ausentes ou alterados pelo empacotador;
- create, read, rotate, delete e ausência após limpeza no store nativo real,
  sob namespace exclusivo, sem ler/listar entradas pessoais;
- locked, denied/cancelled, missing-bus, missing-service e coleção ausente em
  ambiente descartável autorizado; nenhum auto-unlock/auto-create/fallback;
- Null, fail-only, classes reais `keyrings.alt` plaintext/encrypted, ambiente,
  propriedades, classe inesperada/nome falsificado e chain não aprovada;
  confirmar recusa específica, zero chamadas de escrita e zero arquivo/config;
- comportamento em atualização do pacote/identidade, reconexão, cleanup após
  erro e falhas redigidas. Arquivar timestamps UTC, SO, pacote/hash, versões,
  códigos e resultados sanitizados.

Runner sem sessão segura fica fora da matriz suportada. Skips, mocks, Null,
memória e evidência de outro alvo não satisfazem esse gate. Testes unitários
deste PR protegem ferramentas de pesquisa; não convertem o gate em verde.

O mantenedor deve registrar explicitamente o aceite humano, datado e com a
matriz/evidência e riscos aceitos, antes de mudar `Proposed`. A saída anexada
pelo revisor precisa passar revisão das fontes/licenças e todos os casos acima;
um CRUD bem-sucedido isolado não basta. Uma issue posterior implementará port,
`SecretRef`, adapter e composição somente depois desse aceite. Esta issue não
autoriza esses tipos, UI, autenticação, OAuth, migração, cloud vaults ou
dependência de produção.

## Alternativas e riscos restantes

- Instalar keyring e aceitar sua seleção automática: rejeitado, pois ambiente,
  configuração, prioridade e chains podem alterar o backend.
- KWallet: adiado; sem sessão/prova/inventário específicos. Compatibilidade
  com Linux ou protocolo não autoriza substituição de GNOME Keyring.
- Windows e macOS: adiados por falta de hosts/pacotes reais, sem inferência a
  partir da documentação upstream.
- Arquivo criptografado, plaintext, ambiente, Null ou fail como fallback:
  rejeitados; mudariam a ameaça e a retenção sem autorização.
- Continuar sem persistência de credenciais: comportamento atual preservado e
  recomendação operacional até fechar evidências e aceite.

Riscos em aberto: integridade/identidade do serviço no mesmo usuário, corridas
de estado, risco do pacote de desenvolvimento observado, limites de limpeza
em perda de sessão, versão pública atual não consultada, licenças transitivas
nativas incompletas e efeitos de packaging não medidos. Não são riscos
implicitamente aceitos pela existência deste ADR.
