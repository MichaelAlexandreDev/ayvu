# Performance and request control

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

### Perfis de performance

O comando `translate` aceita `--performance-profile` para aplicar presets de execução:

```bash
uv run ayvu translate livro.epub --target pt --performance-profile low
uv run ayvu translate livro.epub --target pt --performance-profile high
```

Os perfis disponíveis são:

| Perfil | Uso recomendado | Configuração |
|---|---|---|
| `low` | Máquinas fracas ou servidor instável | `workers=1`, `requests_per_second=1`, `chunk_limit=1500`, retries mais pacientes |
| `standard` | Padrão seguro | `workers=1`, sem limite de taxa, `chunk_limit=3000`, retries padrão |
| `high` | Máquina e servidor estáveis | `workers=4`, sem limite de taxa, `chunk_limit=4000`, retries padrão |
| `custom` | Ajuste manual por flags | Começa do padrão `standard` e usa os overrides informados |

`standard` é o padrão quando `--performance-profile` não é informado. Configurações mais
fortes, como `high`, são sempre opt-in. Flags explícitas continuam tendo precedência sobre
o perfil: por exemplo, `--performance-profile high --workers 2` usa o restante do perfil
`high`, mas limita os workers a `2`.

No momento, `high` é incompatível com `--translation-memory`, porque esse recurso exige
`--workers 1` nesta versão.

### Controle de requisições ao tradutor

Por padrão, o Ayvu não limita a taxa de requisições ao tradutor local. Para servidores mais
fracos ou instáveis, use `--requests-per-second` para limitar quantas chamadas HTTP podem
começar por segundo:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --url http://localhost:5000 \
  --requests-per-second 2
```

Retries são controlados por `--retries`. Falhas de conexão, timeout, HTTP `429` e HTTP `5xx`
podem ser tentadas novamente. O atraso entre tentativas usa backoff exponencial: começa em
`--retry-backoff` segundos, por padrão `0.5`, e é limitado por `--retry-backoff-max`, por
padrão `8.0`.

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --retries 4 \
  --retry-backoff 1 \
  --retry-backoff-max 10
```

O `test-translator` envia o texto `Hello world` ao servidor, e só esse texto; nenhum outro comando
envia conteúdo para testar o tradutor. No modo desenvolvedor, os erros de `test-translator`,
`languages` e da verificação inicial mostram um código, como `provider_timeout`, em vez da
mensagem do servidor, que pode conter a URL ou parte da resposta.

As mesmas opções também existem em `test-translator` e `languages`, para testar o servidor com
os controles que serão usados na tradução.

Para processar vários documentos internos do EPUB em paralelo, use `--workers`.
O padrão é `--workers 1`, que mantém a execução sequencial conservadora. Com
`--workers` maior que `1`, o Ayvu cria uma instância de tradutor e uma conexão
SQLite separadas por worker, aplica os resultados na ordem original dos capítulos
e preserva a ordem do EPUB, do relatório e do CSV de revisão. Se
`--requests-per-second` estiver ativo, o limite é compartilhado entre os workers.
O plano de tradução mostra `Workers`, `Rate limit` e `Retry policy` antes de
começar; com execução paralela, a barra de capítulos também indica que o
progresso é reportado na ordem do EPUB.

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --workers 2 \
  --requests-per-second 2
```

Sem `--output`, o Ayvu salva por padrão em:

```text
~/Documentos/Livros/Traduzidos/livro-pt.epub
```

No **Modo Comum**, o Ayvu mostra a pasta padrão de saída, o nome calculado para o EPUB
traduzido e pergunta se você deseja manter esse local antes de iniciar a tradução. Se preferir
outro caminho, responda não à pergunta e informe o caminho desejado. No **Modo Desenvolvedor**,
use `--output` para escolher manualmente o caminho da saída:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub
```

Usar glossário no modo comum:

```bash
uv run ayvu
```

Escolha `Glossaries` para criar ou editar um glossário guiado. Na criação, o Ayvu pede o termo original,
permite informar uma tradução preferida ou preservar o termo sem tradução, mostra uma prévia,
valida o conteúdo e salva o JSON em:

```text
$XDG_CONFIG_HOME/ayvu/glossaries
```

Quando `XDG_CONFIG_HOME` não estiver definido, o fallback é:

```text
~/.config/ayvu/glossaries
```

Se houver glossários salvos, o fluxo guiado de tradução permite escolher um deles antes de
confirmar a saída do EPUB.

O catálogo guiado aceita até 1.000 arquivos JSON, cada um com até 1 MiB, 10.000
entradas, termos de até 1.024 caracteres e traduções de até 4.096 caracteres; o
JSON salvo também tem limite de 1 MiB. Arquivos inválidos, grandes demais ou com
links simbólicos ou múltiplos links físicos não podem ser selecionados: escolha
outro glossário ou corrija o arquivo fora do fluxo guiado. Uma edição é recusada se o arquivo mudou depois de aberto; nesse caso, abra-o de novo.
O conteúdo e os nomes dos glossários são privados. Essas regras se aplicam ao
catálogo gerenciado; caminhos explícitos de `--glossary` continuam sendo lidos
pelo carregador avançado existente.

Usar glossário no modo desenvolvedor:

```bash
cp glossary.example.json glossary.json

uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --glossary glossary.json
```

No **Modo Comum**, se a saída já existir, o Ayvu mostra o caminho calculado e permite
sobrescrever, escolher outro nome ou cancelar sem alterar o arquivo existente. Para pular a
pergunta e sobrescrever direto:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --overwrite
```

Simular uma tradução sem gravar EPUB:

```bash
uv run ayvu translate livro.epub \
  --output teste.epub \
  --dry-run
```

Reconstruir usando apenas o cache, sem chamar o tradutor:

```bash
uv run ayvu translate livro.epub \
  --output teste.epub \
  --cache-only \
  --missing-output faltantes.txt
```

Ao final da tradução, o Ayvu mostra um relatório no terminal com o EPUB original, idiomas, saída calculada, arquivo de revisão quando solicitado, capítulos processados, textos traduzidos, cache, textos reaproveitados da memória de tradução e sugestões de revisão quando a memória está ativa, textos ausentes do cache (no modo cache-only), erros e resumo do uso do glossário quando houver glossário configurado. No **Modo Comum**, também pergunta se deve salvar esse relatório em Markdown em `~/Documentos/Livros/Relatorios`. Em traduções em batch, o Ayvu salva automaticamente um relatório Markdown separado para cada livro.

Extrair texto visível para Markdown:

```bash
uv run ayvu extract livro.epub \
  --output livro-extraido/
```

Sem `--overwrite`, um diretório de saída não vazio é recusado. Com a opção, o
Ayvu substitui apenas os Markdown planejados e preserva os demais arquivos. Cada
Markdown é publicado atomicamente; se uma falha ocorrer depois de algum arquivo
ser publicado, a mensagem informa quantos foram concluídos e quantos não foram.
Essa publicação é independente por arquivo, sem transação para o diretório todo.
