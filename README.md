# ayvu

`ayvu` é uma CLI em Python para traduzir arquivos EPUB locais usando um tradutor HTTP local compatível com LibreTranslate. A ferramenta preserva a estrutura interna do livro e modifica apenas textos visíveis ao leitor.

O EPUB original nunca é alterado. A saída é gravada em um novo arquivo `.epub`.

## Recursos

- Tradução de documentos XHTML/HTML internos do EPUB.
- Tradução por bloco (parágrafos, títulos e listas) preservando tags internas como `em`, `strong` e links via placeholders.
- Preservação de tags, CSS, imagens, links, sumário e nomes de arquivos internos.
- Cache SQLite para retomar traduções interrompidas e evitar chamadas repetidas.
- Execução paralela opcional por documento com `--workers`, mantendo `1` como padrão conservador.
- Perfis de performance com `--performance-profile` para escolher presets `low`, `standard`, `high` ou `custom`.
- Memória de tradução opcional para reaproveitar trechos parecidos por similaridade, com aplicação ou sugestão por limiar.
- Checkpoints de progresso por capítulo e comando `resume` para retomar com segurança traduções longas.
- Glossário JSON opcional e fluxo guiado para padronizar termos técnicos.
- Perfis de tradução para agrupar idioma de destino, glossário e estilo planejado.
- Proteção de URLs, caminhos, comandos, versões, código inline e identificadores técnicos simples durante a tradução.
- Nome de saída automático baseado no idioma de destino.
- Preview traduzido de uma amostra inicial do EPUB.
- Modo comum guiado e modo desenvolvedor direto.
- Biblioteca inicial para listar originais e traduções e abrir EPUBs no leitor configurado ou padrão do sistema.
- Checagens internas antes de iniciar traduções reais.
- Modo `dry-run` para simular o processamento sem gerar arquivo.
- Modo `cache-only` para reconstruir o EPUB usando apenas o cache, sem chamar o tradutor.
- Extração de texto visível para Markdown.
- Relatório final no terminal e opção de salvar relatório Markdown no modo comum.
- Validação do EPUB gerado com barra de progresso, avisando sobre capítulos vazios, links internos quebrados e imagens referenciadas ausentes.

## Aviso de Uso

Este projeto é destinado a uso pessoal, estudo e acessibilidade com arquivos EPUB fornecidos por você. Ele não remove DRM, não baixa livros e não deve ser usado para distribuir conteúdo protegido por copyright.

Por padrão, arquivos `.epub`, cache local e glossários privados ficam fora do Git.

## Requisitos

- Python 3.11+
- `uv` ou `pip`
- Um servidor local compatível com LibreTranslate

## Instalação Com uv

Dentro do diretório do projeto:

```bash
cd ayvu
uv sync --extra dev
```

Execute os comandos sem ativar manualmente o ambiente virtual:

```bash
uv run ayvu --help
```

## Instalação Com pip

```bash
cd ayvu
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Com o ambiente ativado:

```bash
ayvu --help
```

## Servidor de Tradução

Suba o LibreTranslate localmente com Docker:

```bash
docker run -it -p 5000:5000 libretranslate/libretranslate
```

Endpoint usado pela CLI:

```text
http://localhost:5000/translate
```

Teste a conexão:

```bash
uv run ayvu test-translator --url http://localhost:5000
```

## Modos de Uso

O Ayvu possui dois modos de execução que equilibram facilidade de uso com eficiência técnica:

- **Modo Comum (common)**: Focado em uma experiência guiada. Oferece sugestões de retomada de traduções interrompidas, convites para gerar previews e solicita confirmações antes de ações importantes (como sobrescrever arquivos). É o modo padrão ao executar apenas `ayvu`.
- **Modo Desenvolvedor (developer)**: Focado em execução direta e automação. Pula perguntas interativas e assume as configurações padrão ou passadas via argumentos. É o modo padrão ao utilizar subcomandos como `translate` ou `inspect`.

Para um passo a passo dos dois fluxos, leia o
[`tutorial de modo comum e modo desenvolvedor`](docs/tutorial-modo-comum-e-dev.md).

Você pode forçar um modo específico usando a opção global `--mode`:

```bash
uv run ayvu --mode common translate livro.epub
```

Liste os idiomas disponíveis no LibreTranslate local:

```bash
uv run ayvu languages --url http://localhost:5000
```

## Uso

Inspecionar um EPUB:

```bash
uv run ayvu inspect livro.epub
```

Traduzir um EPUB:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --translator libretranslate \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite
```

Traduzir usando um perfil salvo na configuração:

```bash
uv run ayvu translate livro.epub --profile technical
```

Gerenciar o cache de traduções:

```bash
uv run ayvu cache inspect --cache .cache/traducoes.sqlite
uv run ayvu cache clean --cache .cache/traducoes.sqlite --source en --target pt --dry-run
uv run ayvu cache export cache-ayvu.json --cache .cache/traducoes.sqlite
uv run ayvu cache import cache-ayvu.json --cache .cache/traducoes.sqlite
```

Retomar uma tradução interrompida a partir do checkpoint salvo:

```bash
uv run ayvu resume
uv run ayvu resume livro.epub --target pt
```

Traduzir vários EPUBs em uma única execução:

```bash
uv run ayvu translate livro-1.epub livro-2.epub livro-3.epub \
  --target pt \
  --output-dir traduzidos/ \
  --continue-on-error
```

No batch, cada livro gera um EPUB separado com o padrão
`<nome>-<idioma>.epub` dentro de `--output-dir`. Se `--output-dir` não for
informado, o Ayvu usa a pasta de traduzidos configurada. O comando para no
primeiro erro por padrão; com `--continue-on-error`, continua nos livros
restantes e termina com código 1 se algum item falhar. Saídas existentes não
são sobrescritas sem confirmação no modo comum ou sem `--overwrite` no modo
desenvolvedor.

As opções `--output` e `--review-output` são exclusivas do fluxo de um único
EPUB, porque um caminho único de saída ou CSV não é suficiente para representar
vários livros. O batch salva automaticamente um relatório Markdown separado por
livro na pasta de relatórios configurada.

Traduzir apenas capítulos selecionados:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --chapters "1-3,5,*chapter2*"
```

`--chapters` aceita índices 1-based, faixas e padrões separados por vírgula.
Padrões com `*`, `?` ou `[` usam glob simples; sem curinga, o Ayvu faz busca
por trecho. A seleção é comparada com o título detectado, nome do item e caminho
interno do documento no EPUB. Antes de traduzir, o Ayvu mostra a tabela
`Selected chapters`. Capítulos não selecionados são copiados sem mudanças para o
EPUB de saída.

Se `--source` não for informado, o Ayvu lê o idioma do EPUB nos metadados, exibe o
plano da tradução (`From`/`To`) antes de começar e usa o idioma detectado como
origem. Quando o metadado estiver ausente ou inválido, o Ayvu avisa e usa `en`
como padrão; informe `--source` para escolher outro idioma de origem.

Por padrão, o Ayvu preserva metadados do EPUB e o documento de navegação. Para
traduzir também o título do livro no OPF e o texto do sumário EPUB3, use:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --translate-metadata
```

Essa opção é conservadora: traduz o primeiro `dc:title` e a navegação marcada no
manifesto como `properties="nav"`, preservando identificadores, autores, editora
e metadados técnicos. Alterar o título ou o sumário pode afetar a forma como
leitores e bibliotecas organizam o livro; confira o EPUB gerado antes de usar a
tradução como cópia principal.

Por padrão, o Ayvu preserva o texto alternativo (`alt`) das imagens. Para também
traduzir essas descrições de acessibilidade, use:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --translate-alt-text
```

Essa opção traduz apenas o atributo `alt` das tags `<img>`, preservando a imagem,
o `src` e os demais atributos. Imagens decorativas (`alt=""`) são ignoradas. O
relatório passa a mostrar quantos textos alternativos foram traduzidos. Ler o
texto que está dentro da imagem (OCR) está fora do escopo e fica para uma feature
futura.

Gerar um CSV opcional para revisão humana externa:

```bash
uv run ayvu translate livro.epub \
  --target pt \
  --review-output livro-review.csv
```

O arquivo de revisão só é criado quando `--review-output` é informado. Ele
registra cada segmento traduzido com identificador de capítulo e segmento,
caminho interno do documento no EPUB, idiomas, origem do cache e texto original
e traduzido lado a lado. Se o CSV já existir, use `--overwrite` ou escolha outro
caminho. A opção não funciona com `--dry-run`, porque o dry-run não gera texto
traduzido revisável.

Depois de revisar o CSV (editando a coluna `translated`), reconstrua um EPUB
final a partir do EPUB original com o texto revisado:

```bash
uv run ayvu apply-review livro.epub livro-review.csv \
  --output livro-revisado.epub
```

O comando `apply-review` lê o EPUB original e o CSV revisado, confere que cada
segmento ainda corresponde ao livro (comparando o texto original) e aplica as
traduções revisadas em um novo EPUB, sem alterar o original. Se `--output` for
omitido, o padrão é `<nome>-<idioma>-reviewed.epub`; use `--overwrite` para
substituir um arquivo existente. O relatório informa quantos segmentos foram
aplicados e lista trechos sem revisão, ausentes no EPUB, inconsistentes (texto
original divergente), com identificadores duplicados ou documentos
desconhecidos.

Limitação: o CSV de revisão guarda apenas o texto visível de cada segmento.
Por isso, formatação inline dentro de um parágrafo traduzido (negrito, itálico,
links) é convertida em texto plano ao aplicar a revisão. A estrutura de blocos
do EPUB (capítulos, parágrafos, imagens, sumário) é preservada.

Gerar um preview traduzido:

```bash
uv run ayvu --preview livro.epub
```

O preview traduz os primeiros documentos internos do EPUB, preserva o restante da estrutura e
salva por padrão em:

```text
~/Documentos/Livros/Preview/livro-preview.epub
```

No primeiro uso do modo comum, o Ayvu pergunta o idioma padrão de leitura/tradução,
a pasta base dos livros e mostra os nomes das pastas das funcionalidades. Você pode
manter os nomes padrão ou alterá-los uma única vez antes de salvar a configuração.
Nas próximas execuções esse idioma é usado como destino padrão em traduções e
previews, e a pasta base organiza biblioteca, previews, relatórios e traduções, sem
perguntar de novo.

Ao executar apenas `uv run ayvu`, o Ayvu abre um primeiro menu guiado com opções para traduzir
livro, gerar preview, abrir biblioteca, gerenciar glossários, acessar configurações, mostrar ajuda ou sair. A biblioteca
lista EPUBs das pastas `Original` e `Traduzidos`, mostra as versões disponíveis de cada livro e
permite abrir o original ou uma tradução no leitor configurado ou no leitor padrão detectado no
sistema. A opção `Settings` permite ver e alterar idioma padrão, pasta base dos livros, nomes das
pastas das funcionalidades, app leitor de EPUB e perfis de tradução configurados. Nos fluxos
guiados de tradução e preview, o Ayvu mostra o idioma de destino padrão salvo como primeira
opção. Ao escolher `Outro idioma`, ele lista os idiomas informados pelo LibreTranslate com nome,
código e estado, permitindo selecionar pela opção exibida ou digitar um código.
No modo desenvolvedor, o idioma de destino continua sendo definido por `--target`.

Se perfis estiverem configurados, o fluxo guiado de tradução permite escolher um perfil antes do
idioma de destino. O perfil pode fornecer um idioma padrão diferente e um glossário associado.
No modo desenvolvedor, use `--profile nome`. Flags explícitas como `--target` e `--glossary`
sobrescrevem os valores do perfil.

Antes de iniciar a tradução, o Ayvu verifica internamente o par de idiomas, o glossário, o cache, o EPUB de entrada e, em traduções reais, o tradutor configurado. Se algo impedir a execução, o comando falha cedo com uma mensagem curta e um próximo passo.

O Ayvu também resolve a rota de tradução consultando os idiomas do LibreTranslate: se não houver caminho direto entre origem e destino, tenta uma rota intermediária via inglês (por exemplo `fr -> en -> pt`). Quando a rota intermediária é usada, o modo comum avisa que a tradução passará por 2 etapas e que a qualidade pode ficar comprometida; o modo desenvolvedor mostra a rota explicitamente. Se nenhuma rota estiver disponível, o comando falha antes de processar o EPUB.

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

Escolha `Glossaries` para criar ou editar um glossário guiado. O Ayvu pede o termo original,
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

## Glossário

O glossário aceita o formato simples de pares de termos, mantido por compatibilidade:

```json
{
  "Game Loop": "loop de jogo",
  "Observer": "Observer"
}
```

Também aceita regras explícitas por termo:

```json
{
  "Game Loop": {
    "rule": "translate",
    "translation": "loop de jogo",
    "required": true
  },
  "Observer": {
    "rule": "preserve",
    "required": true
  },
  "AntiPattern": {
    "rule": "forbidden"
  }
}
```

Use `translate` para definir uma tradução preferida e `preserve` para manter um termo padronizado no texto final. Use `required: true` em regras `translate` ou `preserve` quando o termo esperado deve aparecer na saída traduzida. A regra `forbidden` marca termos que não devem aparecer na saída.

Quando um glossário é usado, o relatório final conta quantas aplicações de `translate` e `preserve` ocorreram, inclusive em textos vindos do cache. O relatório também avisa termos obrigatórios ausentes e termos proibidos encontrados na saída.

Use `Glossaries` no modo comum para criar glossários guiados ou `glossary.example.json`
como base para o modo desenvolvedor. O arquivo `glossary.json` local e os glossários privados
são ignorados pelo Git para evitar versionar preferências pessoais ou conteúdo privado.

Hoje cada tradução usa no máximo um glossário ativo: o arquivo escolhido em `--glossary`, o
glossário associado ao perfil, ou nenhum glossário. O Ayvu não aceita vários `--glossary` na
mesma execução. A decisão atual evita conflitos silenciosos entre arquivos, como um termo
marcado para tradução em um glossário e para preservação ou proibição em outro.

A direção planejada, se a necessidade crescer, é preferir glossários por regra em vez de
empilhamento genérico. Nesse modelo futuro, um arquivo poderia declarar no início que todos os
seus termos são `translate`, `preserve` ou `forbidden`, reduzindo repetição para quem mantém
glossários grandes. Mesmo nesse caso, a implementação deve definir antes regras claras de
prioridade, conflito e relatório.

Antes de enviar cada trecho ao tradutor, o Ayvu protege termos especiais como URLs, caminhos de arquivo, comandos de terminal, versões como `v1.2.0`, código inline entre crases, placeholders e identificadores técnicos simples. Esses termos são restaurados antes da aplicação do glossário e antes de salvar a tradução no cache.

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

O arquivo exportado inclui texto original e texto traduzido. Trate esse JSON
como conteúdo privado do livro, da mesma forma que o EPUB e o cache SQLite.

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
[contrato do manifesto local](docs/architecture/project-manifest.md).
O repositório também oferece uma API explícita para resolver o banco local
`projects/<UUID>/project.sqlite` e seus backups em `backups/schema/`.
O bootstrap SQLite e as migrações são chamados pela composição quando necessários;
a tradução atual continua sem criar automaticamente esse banco. O manifesto v1
e o cache independente permanecem preservados. Consulte o
[protocolo de migração e recuperação](docs/architecture/project-migrations.md)
para backup verificado, conflitos, queda de processo e recuperação manual.
Esse banco também tem o [registro durável de jobs](docs/architecture/job-lifecycle.md),
e o [registro de artefatos e checkpoints](docs/architecture/job-artifacts.md), que
ainda não são usados pela tradução nem pela retomada: elas continuam funcionando
como descrito abaixo.
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

## Memória de Tradução

O cache normal só reaproveita traduções de trechos **idênticos**. A memória de
tradução é uma camada opcional que também reaproveita trechos **parecidos**, útil
em livros técnicos, séries e textos com frases repetidas. Ela fica **desligada por
padrão** e é ativada com `--translation-memory`:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --cache .cache/traducoes.sqlite \
  --translation-memory \
  --tm-apply-threshold 0.95 \
  --tm-suggest-threshold 0.80
```

A memória usa os mesmos pares original/traduzido já guardados no cache SQLite, em
disco. Para cada trecho novo sem correspondência exata, o Ayvu busca candidatos
parecidos do mesmo par de idiomas e mede a similaridade. O comportamento é em
camadas, controlado por dois limiares de similaridade (entre 0 e 1):

No momento, `--translation-memory` exige `--workers 1`. Para execução paralela,
remova `--translation-memory`; para usar memória de tradução, mantenha o padrão
sequencial.

- **Similaridade ≥ `--tm-apply-threshold`** (padrão `0.95`): a tradução guardada é
  reaproveitada direto, sem chamar o tradutor. Conta como `Texts from memory` no
  relatório.
- **Similaridade em `[--tm-suggest-threshold, --tm-apply-threshold)`** (padrão
  `0.80` a `0.95`): o trecho é traduzido normalmente, mas a correspondência
  parecida é registrada como sugestão de revisão e contada em `Memory
  suggestions`.
- **Similaridade abaixo de `--tm-suggest-threshold`**: nada muda, o trecho segue o
  fluxo normal de tradução.

Por segurança, a memória atua **somente em trechos de texto puro**: blocos com
tags inline (como `<em>` ou `<a>`) não reaproveitam memória, e correspondências
que envolvam marcação são descartadas, para nunca injetar a formatação de outro
trecho. A reutilização **não** é regravada no cache sob o novo texto.

Os pares reaproveitáveis são os do próprio cache, então o formato exportável já
existente serve à memória: use `ayvu cache export` para salvar os pares
original/traduzido em JSON.

> **Risco de qualidade (falsos positivos).** Trechos parecidos podem ter
> significados diferentes (negações, números, nomes, pequenas trocas de palavra).
> Quanto menor o limiar de aplicação, maior o risco de reaproveitar uma tradução
> errada. Por isso a memória é opt-in e usa limiares altos por padrão; o nível de
> sugestão existe justamente para que correspondências menos seguras sejam
> revisadas em vez de aplicadas automaticamente. Para um livro novo e sensível,
> prefira manter a memória desligada ou usar `--review-output` para conferir o
> resultado.

## Biblioteca

A biblioteca inicial usa a pasta base configurada, por padrão:

```text
~/Documentos/Livros
```

Com os nomes padrão, os EPUBs ficam organizados assim:

```text
~/Documentos/Livros/
├── Original/
└── Traduzidos/
```

Coloque EPUBs originais em `Original` e traduções finais em `Traduzidos`. Ao escolher
`Open library` no menu comum, o Ayvu lista os livros em ordem alfabética, mostra se existe
original e quais traduções foram encontradas, permite ver informações do livro e abre a versão
selecionada no app leitor. Se nenhum leitor padrão for detectado, configure o comando do leitor em
`Settings` no campo `Reader app`.

## Configuração

O Ayvu define um formato inicial para preferências locais. No modo comum, o primeiro uso pergunta o
idioma padrão e salva a configuração. Depois, a opção `Settings` permite alterar idioma padrão,
pasta base dos livros, nomes das pastas das funcionalidades e app leitor de EPUB. O arquivo fica em:

```text
$XDG_CONFIG_HOME/ayvu/config.json
```

Quando `XDG_CONFIG_HOME` não estiver definido, o fallback é:

```text
~/.config/ayvu/config.json
```

Formato inicial:

```json
{
  "version": 1,
  "default_target_language": "pt",
  "books_dir": "~/Documentos/Livros",
  "folders": {
    "original": "Original",
    "translated": "Traduzidos",
    "preview": "Preview",
    "reports": "Relatorios",
    "processing": "Processando"
  },
  "reader_app": null,
  "profiles": {}
}
```

Perfis podem ser adicionados manualmente em `profiles`:

```json
{
  "profiles": {
    "technical": {
      "target_language": "pt",
      "glossary": "technical.json",
      "style": "technical"
    }
  }
}
```

`target_language` define o idioma de destino padrão quando `--target` não for informado.
`glossary` aceita caminho absoluto ou caminho relativo à pasta local de glossários. `style`
aceita `neutral`, `technical`, `literary` ou `academic`, mas no backend LibreTranslate atual
serve apenas como metadado do perfil; ele não muda o prompt nem o comportamento do tradutor.

A precedência para campos cobertos por perfis de tradução é:

```text
argumentos da CLI > perfil selecionado > arquivo de configuração > padrões internos
```

Sem caminhos explícitos, a pasta base e os nomes de pastas configurados definem onde o Ayvu salva
previews, traduções, relatórios Markdown e estados de processamento, além das pastas usadas pela
biblioteca. O campo `reader_app` define o comando usado para abrir EPUBs pela biblioteca quando o
leitor padrão do sistema não for suficiente.

Glossários criados pelo modo comum ficam ao lado da configuração, em `glossaries/`, e podem ser
selecionados antes de iniciar uma tradução guiada.

A configuração continua no formato v1. Leituras não regravam o arquivo; alterações
usam gravação atômica e uma revisão para impedir que duas sessões sobrescrevam
mudanças uma da outra. Campos desconhecidos permitem leitura com aviso, mas
bloqueiam gravações até revisão explícita do arquivo, evitando perda de dados.
Se uma gravação falhar, o menu informa que os valores valem apenas para a sessão
atual. Em conflito de revisão, recarregue a configuração antes de tentar salvar
novamente. Comandos do leitor com opções de credenciais, como `--token` ou
`--password`, são recusados; a configuração não é um armazenamento de segredos.
Consulte o [contrato de preferências](docs/architecture/global-preferences.md)
para limites, resultados de erro, compatibilidade e permissões.
Arquivos inválidos, futuros ou acima dos limites são preservados: guardar uma
cópia privada e corrigir explicitamente o arquivo é necessário antes de salvar
novamente. O menu não substitui esses arquivos pela configuração padrão.

## Testes

```bash
uv run pytest
```

## Fluxo do Projeto

O fluxo de trabalho do projeto usa issue por tarefa, branch curta por tarefa,
pull request para merge na `main`, milestone por versao e tag com GitHub Release
ao publicar. O passo a passo esta em
[`docs/release-workflow.md`](docs/release-workflow.md).

## Estrutura

```text
ayvu/
├── docs/
│   ├── release-workflow.md
│   ├── relatorio-tecnico.md
│   └── tutorial-modo-comum-e-dev.md
├── src/
│   └── ayvu/
├── tests/
├── glossary.example.json
├── pyproject.toml
├── README.md
└── uv.lock
```

## Limitações

- EPUBs com XHTML malformado podem depender do comportamento do parser.
- Livros técnicos costumam exigir glossário para manter termos consistentes.
- A qualidade final depende do servidor de tradução usado.
