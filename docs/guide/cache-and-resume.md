# Cache, resume and cache-only

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Cache e Retomada

As traduções são armazenadas em SQLite. Se o processo for interrompido, rode o mesmo comando novamente usando o mesmo arquivo de cache:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --cache .cache/traducoes.sqlite
```

Trechos já traduzidos serão reaproveitados automaticamente.
O checkpoint de retomada preserva as opções de execução do tradutor, como
timeout, workers, retries, limite de requisições por segundo e backoff.

A tradução pela CLI usa o cache legado v1; o caminho rico dispõe de cache v2 independente. A migração preserva os dados v1 sem transformá-los em acertos exatos v2. A administração aceita namespaces explícitos; sem seleção, mantém o comportamento legado. Veja [cache standalone v2](../architecture/modules/knowledge-cache.md).

O cache pode ser inspecionado, limpo, exportado e importado separadamente dos
comandos de tradução:

```bash
uv run ayvu cache inspect --cache .cache/traducoes.sqlite
uv run ayvu cache clean --cache .cache/traducoes.sqlite --source en --target pt --dry-run
uv run ayvu cache clean --cache .cache/traducoes.sqlite --source en --target pt --yes
uv run ayvu cache export cache-ayvu.json --cache .cache/traducoes.sqlite
uv run ayvu cache import cache-ayvu.json --cache .cache/traducoes.sqlite
```

`cache inspect` agrupa entradas por idioma de origem, idioma de destino,
quantidade e datas da primeira e última entrada. `cache clean` aceita filtros
por `--source`, `--target` e `--before`; para limpar tudo, use `--all`. A
remoção real exige `--yes`, e `--dry-run` mostra quantas entradas seriam
apagadas sem modificar o SQLite. `cache export` grava JSON legível e
`cache import` lê esse JSON; entradas existentes são puladas por padrão e podem
ser substituídas com `--replace`.

Se o cache ainda não existir, `inspect` mostra zero entradas e `export` grava
um JSON vazio sem criar o cache nem sua pasta. A limpeza rejeita uma seleção
que mudou entre a prévia e a remoção; nesse caso, execute o comando novamente.
Erros detalhados usam códigos curtos, sem incluir conteúdo das entradas.
O destino da exportação deve ser um arquivo diferente do próprio cache SQLite.
No formato legado v1, exportação e importação têm limite de 512 MiB e 1.000.000
de entradas; acima disso, exporte em partes com `--source`, `--target` ou
`--before`. O formato v2 tem limites e filtros próprios descritos em
[administração de cache](../architecture/modules/knowledge-cache.md).

O arquivo exportado inclui texto original e texto traduzido. Trate esse JSON
como conteúdo privado do livro, da mesma forma que o EPUB e o cache SQLite.

`cache import` prepara as entradas num arquivo temporário ao lado do cache,
`.ayvu-cache-import-legacy-*.sqlite`, e só depois altera o cache. Um Ctrl+C
apaga esse arquivo. Se o processo for morto à força ou o computador desligar no
meio, o cache fica como estava, mas o arquivo temporário sobra: ele contém
textos do livro e pode ser apagado quando nenhum import estiver rodando.

A administração de cache v2 aceita seleção explícita de namespace e importação com mapa externo e previsão. Veja [administração de cache](../architecture/modules/knowledge-cache.md) para exemplos, autorização de escopos e limites. Arquivos de exportação e recuperação podem conter textos sensíveis.

Traduções diretas, guiadas e em lote criam ou reutilizam automaticamente um
projeto local em `<pasta de processamento>/projects/<UUID>/project.json`.
O manifesto guarda identidade, referência e hash do original, idiomas e
referências de configuração. Ele contém metadados privados, não o texto do livro
nem credenciais. Mudar idioma ou configuração cria outro projeto, preservando
o anterior. Prompts, nomes de saída e o EPUB original permanecem preservados.
`--cache-only` também resolve um projeto; `--dry-run`, previews e a retomada de
checkpoints antigos continuam sem exigir manifesto. A pasta de processamento
padrão é `~/Documentos/Livros/Processando`.

Falhas de persistência ou ambiguidades interrompem a tradução antes do
preflight do tradutor. Não há limpeza automática: remover estado de projeto
nunca apaga automaticamente originais, saídas, cache ou glossários, mas perde
a identidade e as referências necessárias ao futuro histórico/retomada por
projeto. Links simbólicos e fontes com hardlinks são recusados nesse fluxo.
Para schema, limites e recuperação de escrita interrompida, consulte o
[contrato do manifesto local](../architecture/modules/projects.md).
O repositório também oferece uma API explícita para resolver o banco local
`projects/<UUID>/project.sqlite` e seus backups em `backups/schema/`.
O bootstrap SQLite e as migrações são chamados pela composição quando necessários;
a tradução com projeto cria esse banco no primeiro uso. O manifesto v1
e o cache independente permanecem preservados. Consulte o
[protocolo de migração e recuperação](../architecture/modules/projects-storage.md)
para backup verificado, conflitos, queda de processo e recuperação manual.
Esse banco também tem o [registro durável de jobs](../architecture/modules/jobs.md),
o [registro de artefatos e checkpoints](../architecture/modules/artifacts.md) e o
[registro de proveniência](../architecture/modules/provenance.md). Cada tradução com projeto
vira um job nesse banco, do começo ao resultado final, e o
[caso de uso](../architecture/modules/translation-project.md) explica o que acontece em cada caso.
Dry-run e preview não têm projeto e não criam job.
Nesse fluxo, a URL do tradutor deve estar livre de usuário/senha, parâmetros de
consulta e fragmentos. Autenticação por referências de credenciais é trabalho
futuro; fingerprints do manifesto continuam sendo metadados privados.

Durante traduções reais, o Ayvu também grava um checkpoint local da execução em
`~/Documentos/Livros/Processando`, ou na pasta de processamento configurada
(arquivo `*.ayvu-state.json`). Além dos caminhos e opções da execução, esse
checkpoint é atualizado **a cada capítulo** e registra o progresso: total de
capítulos, o capítulo atual, os capítulos concluídos, os capítulos com falha e
quantos segmentos falharam. Ele não substitui o cache e não é apagado
automaticamente.

Para retomar uma tradução interrompida sem redigitar as opções, use o comando
`resume`:

```bash
uv run ayvu resume
uv run ayvu resume livro.epub --target pt
```

Sem argumentos, `resume` continua a única tradução em andamento; havendo mais de
uma, informe o EPUB e o `--target` para escolher. O comando mostra o checkpoint
(até onde foi), os controles de execução salvos (`Workers`, `Rate limit` e
`Retry policy`) e reexecuta a tradução com as opções salvas. Ao executar apenas
`uv run ayvu`, o modo comum também procura estados em andamento e oferece retomar
a execução detectada, mostrando o mesmo resumo de checkpoint.

Uma retomada nunca reabre um job que terminou: ela encerra o job interrompido, se
ainda não tiver um resultado, e cria um novo ligado a ele. Ela é recusada se a mesma
tradução ainda estiver rodando em outro processo, se o livro tiver mudado desde o
começo ou se o livro for recusado por política. Um arquivo de estado antigo, sem job
correspondente, continua funcionando como sempre, sem criar projeto nem job. Veja
[recuperação de traduções](../architecture/modules/recovery.md).

O tradutor pode ser chamado de novo para um trecho que estava em andamento quando a
execução foi interrompida. O cache evita repetir o que já foi concluído, mas ainda não
prova qual provedor ou modelo produziu um texto, então toda retomada reprocessa o livro
inteiro em vez de reaproveitar etapas.

Checkpoint e cache se complementam: o **cache** guarda cada trecho já traduzido e
evita retraduzi-lo, enquanto o **checkpoint** registra o progresso e as falhas.
Como o EPUB de saída só é escrito por inteiro ao final, a retomada reprocessa
todos os capítulos, mas, graças ao cache, apenas os segmentos ainda não cacheados
(os que faltaram ou falharam antes) chamam de fato o tradutor — ou seja, a
retomada já reprocessa só o que ficou pendente. Por isso o checkpoint e o cache
devem usar os mesmos caminhos entre as execuções.

### Modo cache-only

O modo `--cache-only` reconstrói o EPUB usando **apenas** o que já está no cache e
**nunca** chama o tradutor. Ele serve para testar a reconstrução, reaproveitar
trabalho anterior de forma controlada e evitar chamadas acidentais ao tradutor.
Como não há tráfego de rede, não é preciso um servidor LibreTranslate ativo.

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --cache .cache/traducoes.sqlite \
  --cache-only \
  --missing-output faltantes.txt
```

Comportamento:

- Trechos presentes no cache são aplicados (com glossário, quando configurado).
- Trechos ausentes do cache ficam no idioma original e são contados como
  `Texts missing` no relatório.
- Por padrão, o EPUB é gerado mesmo com cobertura parcial. Use
  `--require-full-cache` para gerar somente quando todos os trechos estiverem no
  cache; se faltar algum, nada é escrito e o comando falha.
- `--missing-output CAMINHO` salva os trechos originais ausentes do cache. Sem
  essa opção, em cache-only com faltantes, o arquivo é gravado automaticamente na
  pasta de Relatórios. O arquivo é gerado mesmo quando `--require-full-cache`
  bloqueia a saída, para mostrar o que precisa ser traduzido.

Diferença para a retomada normal: a retomada reexecuta a tradução e **chama o
tradutor** para os trechos ainda não cacheados, gravando estado em
`~/Documentos/Livros/Processando`. O cache-only nunca chama o tradutor, não cria
estado de retomada e não exige servidor ativo.

Without a model label, cache-only runs with one worker: `--workers` above 1, or a profile such as
`high`, is refused with a clear message. With a label, cache-only runs with several workers.
