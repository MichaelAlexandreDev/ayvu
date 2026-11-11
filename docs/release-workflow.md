# Fluxo de Issues, Branches e Releases

Este documento descreve o fluxo padrao usado no Ayvu para organizar tarefas, revisao e publicacao de versoes.

## Fluxo Padrao

1. Criar ou selecionar uma issue para a tarefa.
2. Criar uma branch curta e descritiva a partir da `main`.
3. Implementar a mudanca com commits pequenos e coerentes.
4. Abrir um pull request para merge na `main`.
5. Validar a mudanca com testes ou checagens adequadas.
6. Fazer merge do pull request.
7. Sincronizar a `main` local e remover a branch da tarefa.
8. Usar milestones, tags e GitHub Releases para organizar versoes.

## Issues

Cada tarefa deve ter uma issue antes da implementacao quando fizer parte do fluxo principal do projeto. A issue deve explicar o objetivo, o comportamento esperado, a motivacao e, quando fizer sentido, uma branch sugerida.

Issues pequenas podem representar correcoes, documentacao, testes, refatoracoes ou funcionalidades. Tarefas grandes devem ser divididas em etapas menores para manter os pull requests revisaveis.

## Branches

Use uma branch por tarefa, criada a partir da `main` atualizada. O nome deve ter um prefixo de tipo e uma descricao curta em kebab-case.

Exemplos:

```text
fix/output-exists-message
docs/release-workflow
ci/pytest-github-actions
feat/internal-environment-check
refactor/cli-progress-module
```

Evite nomes genericos como `feat`, `fix`, `refactor` ou `main2`.

## Pull Requests

Todo merge na `main` deve passar por pull request. A descricao do PR deve deixar claro:

- objetivo;
- o que mudou;
- o que ficou fora do escopo;
- validacao executada;
- issue relacionada.

Use `Refs #N` quando o PR apenas avanca uma issue e `Closes #N` quando o PR conclui a tarefa.

Abra o PR como draft quando ainda houver revisao ou discussao pendente antes do merge.

Commits devem ser pequenos, descritivos e limitados ao escopo da issue. Evite misturar codigo e documentacao nao relacionada na mesma alteracao.

Antes de mergear uma mudanca de codigo, rode a etapa 1 (veja abaixo):

```bash
scripts/check.sh
```

Para mudancas apenas de documentacao, use pelo menos:

```bash
git diff --check
```

## Testes em Etapas e Gate de Qualidade

O repositorio e privado no plano gratuito do GitHub. Nesse plano as Actions tem
cerca de 2.000 minutos por mes, e cada job e cobrado arredondado para o minuto
seguinte. Por isso os testes rodam em tres etapas, e so a ultima usa o GitHub.

### Etapa 1: maquina local, antes de cada push

```bash
scripts/check.sh
```

O script roda `uv lock --check`, sincroniza o ambiente, executa a suite completa
duas vezes (normal e com `GITHUB_ACTIONS=true`, porque o Typer e o Rich mudam a
saida nesse ambiente) e termina com `git diff --check`. Ele para na primeira
falha.

O gancho `pre-push` e opcional e chama o mesmo script. Para instalar:

```bash
ln -s ../../scripts/hooks/pre-push .git/hooks/pre-push
```

Para pular o gancho em um push especifico, use `git push --no-verify`.

### Etapa 2: Docker, sob demanda e antes de cada release

```bash
scripts/docker-tests.sh                 # todos os alvos
scripts/docker-tests.sh fedora:system   # um alvo
```

Os alvos padrao sao Ubuntu 24.04 (Python do sistema, 3.11 e 3.14 via uv),
Debian trixie, Fedora 43 e Arch, cada um com o Python do sistema, mais o alvo
`constrained`, que roda os testes `core` com 1 CPU e 1 GB de memoria.

- As imagens base e o uv sao fixados por digest.
- A imagem recebe apenas `pyproject.toml`, `uv.lock` e `docker/run-tests.sh`
  (veja `.dockerignore`). Nenhum arquivo do projeto fica na imagem.
- O codigo chega no momento da execucao, como um tar dos arquivos que o git
  rastreia ou rastrearia. Tudo o que o `.gitignore` exclui (livros, caches,
  glossarios, `.ayvu-pessoal/`, arquivos de agente) nunca entra no container.
- Os testes rodam sem rede (`--network none`), como usuario comum, e o container
  e removido no fim.

Windows e macOS nao rodam em Docker. Eles entram com o trabalho de release do
Desktop.

### Etapa 3: GitHub Actions

O workflow `.github/workflows/tests.yml` nao publica pacotes nem releases.

| Evento | Jobs | Testes |
| --- | --- | --- |
| Pull request | `ci-gate` | Os testes marcados `core`, no Python 3.11 |
| Push para `main` | `ci-gate` | A suite completa, no Python 3.11 |
| Disparo manual ou tag `v*` | `full-matrix` | A suite completa, no Python 3.11 e no 3.14 |

Todo job tambem verifica o `uv.lock`, gera wheel e source distribution, instala
o wheel isolado e roda `ayvu --help`.

O marcador `core` e aplicado por `CORE_TEST_FILES`, em `tests/conftest.py`. Cada
entrada nomeia uma invariante do projeto (EPUB original intacto, estrutura do
ZIP e `mimetype`, texto visivel e tags, ordem dos chunks, cache, retomada,
glossario, CLI, rede somente local, fronteiras de modulos, links da
documentacao, identidades persistidas, travas de arquitetura e as proprias etapas) e os arquivos de teste que a protegem.
The CLI entry is not a fixed list: every `test_*.py` under `tests/interfaces/cli/` is core, so CLI tests moved out of
`test_cli.py` stay in the pull-request gate.
Para rodar o mesmo conjunto localmente, use `uv run --no-sync pytest -m core`.
`tests/test_ci_stages.py` confere o mapa, os jobs de cada evento, o
`.dockerignore` e este documento. Se a selecao `core` ficar vazia, o pytest sai
com erro e o gate falha.

As Actions sao referenciadas por SHA completo, com a versao legivel em
comentario. O token do workflow tem somente `contents: read`, e o checkout nao
mantem credenciais. Runs antigos do mesmo pull request ou ref sao cancelados e
todos os jobs possuem timeout.

O arquivo `.github/dependabot.yml` agenda atualizacoes semanais para o lock
Python gerenciado por uv e para as Actions. As atualizacoes de versao chegam
agrupadas, uma pull request por ecossistema por semana. As atualizacoes de
seguranca continuam separadas. Nao ha merge automatico nem permissao de escrita
no CI.

### Travas de arquitetura

Tres checagens fazem parte do conjunto `core` (REF-03, #271):

- `tests/test_size_ratchet.py`: arquivos ate 400 linhas e funcoes ate 80, testes
  incluidos. Os maiores de hoje ficam em `tests/size_ratchet.json` e so podem
  diminuir. Depois de diminuir um deles, rode `scripts/size_ratchet.py --update`.
- `tests/test_import_contracts.py`: roda `lint-imports` com os contratos de
  `pyproject.toml` (`[tool.importlinter]`), que seguem as camadas da decisao de
  estrutura de pacotes. Cada import ignorado nomeia a issue que o remove, e um
  ignore que deixou de existir faz a checagem falhar.
- `tests/test_public_api.py`: um pacote so usa outro pelo que o `__init__.py`
  dele exporta.

### Main sem protecao remota

Num repositorio privado do plano gratuito, a regra que protegia `main` nao e
aplicada pelo GitHub. Nada impede tecnicamente um push direto ou um merge com o
CI vermelho. A regra passa a ser de processo:

1. nunca fazer push direto para `main`;
2. mergear somente pull requests com `ci-gate` verde e a etapa 1 aprovada;
3. como as pull requests rodam so os testes `core`, uma falha fora deles aparece
   no push para `main`; ela e corrigida em seguida, com nova pull request.

## Milestones

Milestones agrupam issues e PRs por versao planejada. Cada milestone deve ter um objetivo pequeno e claro, como polimento de UX, base de manutencao ou robustez inicial.

Ao criar ou revisar issues, associe a milestone mais proxima do objetivo da tarefa quando ela ja existir.

## Tags e GitHub Releases

Quando uma versao estiver pronta:

1. Confira se a `main` local esta sincronizada com `origin/main`.
2. Rode a suite de testes.
3. Crie uma tag seguindo a versao planejada.
4. Publique a tag no GitHub.
5. Crie um GitHub Release com resumo das mudancas, validacao e avisos relevantes.

Exemplo:

```bash
git switch main
git pull
uv lock --check
uv sync --extra dev --locked
uv run --no-sync pytest
git tag v0.0.2
git push origin v0.0.2
```

O release deve mencionar as principais issues fechadas e qualquer limitacao importante para usuarios.

## Limpeza Apos Merge

Depois de mergear um PR:

1. Sincronize a `main` local.
2. Rode os testes se houve mudanca de codigo.
3. Apague a branch local e remota, salvo se houver motivo para mante-la.

Esse fluxo mantem a `main` como linha principal estavel e evita branches antigas sem proposito.

Arquivos de usuario, EPUBs, PDFs, caches SQLite e glossarios privados nao devem
entrar em commits, PRs ou releases.
