# Pesquisa isolada de secret-store — #160

Esta pasta não é um adaptador Ayvu. Leia o
[ADR Proposed](../../docs/architecture/adr-secret-store-adapters.md).
Nenhum alvo tem suporte comprovado. O script importa diretamente um backend,
sem discovery/config de keyring, e nunca importa Ayvu. Seus testes com fakes
não são prova nativa.

## Repetir fora do sandbox

Somente Linux GNOME/Wayland x86_64, na sessão já autorizada: GNOME Keyring já
ativo, coleção padrão preexistente/destrancada, bus de sessão disponível.
Não executar como root nem em contêiner privilegiado. Não trancar, destrancar,
criar ou apagar coleções. Não alterar configuração do serviço. Ausência ou
bloqueio interrompe a prova.

Na raiz do repositório, com rede autorizada para obter pacotes oficiais:

```bash
RESEARCH_ROOT="$(mktemp -d)"
export UV_CACHE_DIR="$RESEARCH_ROOT/uv-cache"
export UV_PROJECT_ENVIRONMENT="$RESEARCH_ROOT/venv"
export UV_PYTHON_DOWNLOADS=never
uv lock --project spikes/secret-store --no-build
uv sync --project spikes/secret-store --locked --no-build
sha256sum spikes/secret-store/uv.lock
"$RESEARCH_ROOT/venv/bin/python" -I spikes/secret-store/probe.py --case inventory --work-root "$RESEARCH_ROOT" > "$RESEARCH_ROOT/inventory.json"
"$RESEARCH_ROOT/venv/bin/python" -I spikes/secret-store/probe.py --case native --work-root "$RESEARCH_ROOT" > "$RESEARCH_ROOT/native.json"
```

O CPython 3.14 deve existir no host. O script lança subprocesso `-I` com HOME,
config/data/cache e cwd temporários, ambiente reduzido, sem loader/proxy/
PYTHONPATH herdados. Somente `DBUS_SESSION_BUS_ADDRESS` passa ao caso `native`;
seu valor não é registrado. Isso não é um sandbox do SO: pacotes confiáveis
executam com autoridade do usuário e acessam o serviço autorizado. Não lê os
diretórios pessoais. Usa apenas o alias padrão preexistente e buscas pelos
atributos do canário criado. Não lista coleções nem itens pessoais.

O namespace e valores são gerados em cada execução. Não são credenciais reais;
não usar conta de provedor, entrada de desenvolvedor ou valor informado pelo
usuário. A limpeza roda em `finally`, inclusive se uma escrita lançar erro,
e verifica ausência sem ler valores. Não matar o processo durante operações.
Perda do serviço, bloqueio durante a prova ou encerramento forçado podem
impedir limpeza: código diferente de zero ou ausência de `cleanup_verified`
impede validar a execução. A ferramenta não persiste os valores nem tenta
destrancar/reconfigurar o serviço para recuperar a limpeza.

Esperado para CRUD válido: exit 0, `outcome=crud_pass`, `native_evidence=true`,
`events=[create,read,rotate,delete,cleanup_verified]`, `isolated_files=0`.
O script usa guardas contra caminhos upstream que abrem prompt ou alteram
coleções; não valida o backend upstream desprotegido. `service_identity` ainda
será `not_attested` e `packaged=false`: sucesso não fecha o ADR.

Executar cada recusa separadamente, guardando stdout e status:

```bash
for RESEARCH_CASE in missing-bus missing-service null fail environment property chain unexpected spoofed; do
  "$RESEARCH_ROOT/venv/bin/python" -I spikes/secret-store/probe.py --case "$RESEARCH_CASE" --work-root "$RESEARCH_ROOT" > "$RESEARCH_ROOT/$RESEARCH_CASE.json"
  echo "$RESEARCH_CASE exit=$?"
done
```

`missing-bus` aponta para socket inexistente na pasta isolada. `missing-service`
usa `/usr/bin/dbus-run-session` e um bus privado sem `org.freedesktop.secrets`;
`GetNameOwner` verifica ausência sem ativar o serviço. Não é simulação de
chaveiro e não modifica o bus da sessão pessoal. O resultado esperado nesse
host é `missing_service`, exit 1. Ambos são provas de indisponibilidade, nunca
provas de CRUD/headless. Aqui o sandbox recusou acesso/inicialização, portanto
esses resultados continuam pendentes.

Null/fail/chain são classes reais, construídas sem inicializador. Nenhuma
operação de armazenamento é chamada antes da recusa por identidade. Classes
inesperadas/nome falsificado são sentinelas sintéticas. Overrides são injetados
no mapa avaliado; discovery nunca os executa. Para testar rejeição já na entrada:

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.SecretService.Keyring "$RESEARCH_ROOT/venv/bin/python" -I spikes/secret-store/probe.py --case native --work-root "$RESEARCH_ROOT"
```

Deve retornar `override_rejected`, exit 1, antes de lançar trabalhador.
Para testar classes reais de `keyrings.alt`, usar **outro ambiente descartável**
com o grupo `rejected` do manifesto. O pin 5.0.2 é exploratório e precisa de
verificação de metadados oficiais/hashes/licenças antes da instalação:

```bash
UV_PROJECT_ENVIRONMENT="$RESEARCH_ROOT/rejection-venv" uv sync --project spikes/secret-store --group rejected --locked --no-build
"$RESEARCH_ROOT/rejection-venv/bin/python" -I spikes/secret-store/probe.py --case plaintext --work-root "$RESEARCH_ROOT"
"$RESEARCH_ROOT/rejection-venv/bin/python" -I spikes/secret-store/probe.py --case alt-encrypted --work-root "$RESEARCH_ROOT"
```

Registrar versão, hashes/licenças e status dessas duas execuções.
A ferramenta já contém esses casos; nesta máquina ambos retornaram
`dependency_missing`, exit 1. Um substituto fake não satisfaz esse item.
O grupo negativo não inclui `keyrings.alt` nos backends candidatos ou produção.

## Execução do revisor (2026-11-06)

Os comandos acima foram executados fora do sandbox, na sessão GNOME
autorizada. Resultado sanitizado em
[`results/reviewer-native-2026-11-06.json`](results/reviewer-native-2026-11-06.json);
lock em [`uv.lock`](uv.lock). `native` retornou `crud_pass` com
`cleanup_verified`; `missing-bus`, `unavailable`; `missing-service`,
`missing_service`; todas as recusas, inclusive as duas classes reais de
`keyrings.alt`, retornaram `backend_rejected` ou `override_rejected`.
Use um `--work-root` curto: com caminho longo, o socket falso de
`missing-bus` passa do limite de 108 bytes do `AF_UNIX` e o caso retorna
`probe_failed`.

## Evidência ainda exigida

Locked/denied/cancelled devem ser observados em sessão/VM descartável
explicitamente autorizada com estado preparado pelo operador. Esta ferramenta
não prepara estado do chaveiro pessoal. Não há comando seguro autorizado nesta
máquina para forçar esses estados; documentar como não observados até existir
esse ambiente. As guardas unitárias são um teste diferente.

Não há empacotador instalado nesta sessão. Antes de liberar qualquer alvo,
construir um executável de pesquisa com a mesma identidade/formato/dependências
do futuro Ayvu, incorporar metadados/licenças, e executar a mesma matriz dentro
dele. O launcher atual é de Python de desenvolvimento; não funciona como
launcher congelado e não deve ser apresentado como evidência de pacote.
Registrar módulos ausentes/alterados, manifesto de arquivos, DSOs, fontes,
licenças e hashes. Nenhum comando de instalador/workflow de produção foi mudado.

O inventário é **do ambiente instalado**, não um resolvedor: inclui metadados,
versões esperadas/observadas, Requires-Dist e textos/hashes de licença.
`matches_pin=false` invalida equivalência ao manifesto. O caso `inventory`
pode retornar 0 mesmo com pacote ausente: sua saída deve ser revisada antes do
gate. Os registros locais mostram um `jaraco.context` de desenvolvimento,
diferente do pin candidato 6.1.2. Resolver e auditar os artefatos exatos é
obrigatório; não copiar bibliotecas do Python do sistema para fingir resolução.

Arquivar saídas sanitizadas revisadas em `results/` junto com timestamp UTC,
plataforma/arquitetura, lock e hashes, hash de pacote (quando existir), tabela
de todos os casos, status e origem das fontes oficiais consultadas. Nunca
anexar dump D-Bus, variáveis de sessão, tracebacks ou entradas pessoais.
Excluir a pasta temporária após revisar/guardar os resultados necessários.

Para reproduzir o inventário nativo da distro Arch, executar (não instala
pacotes, não consulta HOME ou keyring):

```bash
for RESEARCH_PACKAGE in gnome-keyring python-keyring python-cryptography python dbus-broker; do
  pactree --graph --unique "$RESEARCH_PACKAGE" > "$RESEARCH_ROOT/$RESEARCH_PACKAGE.dot"
done
```

Os grafos incluem aliases/provedores (`arrowhead=none`). Para cada nó real,
depois de resolver esses aliases, coletar `LANG=C pacman -Qi <nome>`: versão,
licenças, dependências e origem do pacote. A evidência local
`results/native-closure-2026-11-06.json` contém 190 consultas bem-sucedidas,
grafos completos e mapa de provedores. Não são 190 bibliotecas necessariamente
embarcadas; é o fechamento obrigatório instalado, sem opcionais. Licenças
declaradas pelo gerenciador não substituem auditoria dos textos/notices e
arquivos efetivamente redistribuídos.

## Verificação das ferramentas

```bash
.venv/bin/python -m pytest -q tests/test_secret_store_research.py
git diff --check
```

São testes de isolamento, identidade fechada, overrides, comparação de valores,
limpeza em cada falha, recusa de lock/prompt, redaction e links/status. Não
precisam de dependência de keyring de produção nem leem chaveiro do usuário.
