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

Antes de mergear uma mudanca de codigo, rode as validacoes locais aplicaveis:

```bash
uv lock --check
uv sync --extra dev --locked
uv run --no-sync pytest
```

Para mudancas apenas de documentacao, use pelo menos:

```bash
git diff --check
```

## CI e Gate de Qualidade

O workflow `.github/workflows/tests.yml` valida pushes para `main` e pull
requests, e pode ser disparado manualmente. Ele nao publica pacotes nem
releases. O CI executa:

- verificacao de que `uv.lock` corresponde ao projeto;
- instalacao reproduzivel com a versao de uv fixada;
- suite completa no Ubuntu: Python 3.11 em pull requests, 3.14 em pushes para
  `main` e as duas versoes num disparo manual. A regra do `main` exige a pull
  request atualizada antes do merge, entao o `main` recebe a arvore que ela testou;
- build de wheel e source distribution;
- instalacao isolada do wheel e smoke test de `ayvu --help`;
- job agregado e estavel chamado `ci-gate`.

As Actions sao referenciadas por SHA completo, com a versao legivel em
comentario. O token do workflow tem somente `contents: read`, e o checkout nao
mantem credenciais. Runs antigos do mesmo pull request ou ref sao cancelados e
todos os jobs possuem timeout.

O arquivo `.github/dependabot.yml` agenda atualizacoes semanais e revisaveis
para o lock Python gerenciado por uv e para as Actions. Ele nao habilita merge
automatico nem concede permissoes de escrita ao CI.

Depois que esse workflow estiver em `main`, a protecao remota deve ser feita em
uma acao separadamente autorizada:

1. habilitar Dependabot alerts e Dependabot security updates;
2. proteger `main` contra merge sem pull request;
3. exigir o check estavel `ci-gate` antes do merge;
4. impedir bypasses nao intencionais e confirmar as regras com a API do GitHub.

Essas configuracoes remotas nao existem apenas por adicionar os arquivos ao
repositorio. Ate serem habilitadas no GitHub, o workflow informa falhas, mas nao
impede sozinho um push ou merge em `main`.

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
