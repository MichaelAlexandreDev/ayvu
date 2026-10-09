# Relatório técnico: Ayvu

Este documento registra o estado técnico atual do Ayvu, as decisões principais de arquitetura, os fluxos implementados e os próximos riscos conhecidos.

Última atualização: 2026-05-22.

## 1. Objetivo do projeto

O Ayvu é uma CLI Python para traduzir arquivos EPUB locais usando um tradutor HTTP local compatível com LibreTranslate.

A ferramenta foi pensada para uso pessoal, estudo e acessibilidade. Ela não remove DRM, não baixa livros e não distribui conteúdo protegido. O EPUB original nunca é modificado; a saída é sempre um novo arquivo `.epub`.

Fluxo principal:

```text
arquivo.epub
-> abrir EPUB sem alterar o original
-> localizar documentos XHTML/HTML internos
-> traduzir apenas textos visíveis ao leitor
-> preservar HTML, CSS, imagens, links, sumário e nomes internos
-> usar cache SQLite para reaproveitar traduções
-> copiar o EPUB original substituindo somente documentos traduzidos
-> validar o EPUB gerado com progresso e avisos
```

## 2. Estado atual

O Ayvu já possui:

- tradução de EPUB com preservação da estrutura interna;
- inspeção de EPUB;
- extração de texto visível para Markdown;
- preview traduzido de uma amostra inicial do livro;
- cache SQLite;
- glossário JSON simples;
- chunking de textos longos;
- progresso visual com `rich`;
- preflight antes de traduções reais;
- relatório final no terminal;
- opção de salvar relatório Markdown no modo comum;
- modo comum guiado e modo desenvolvedor direto;
- retomada local de traduções interrompidas;
- comando para listar idiomas do LibreTranslate;
- formato inicial de configuração local com perfis de tradução;
- biblioteca inicial para listar originais e traduções e abrir EPUBs no leitor configurado ou padrão do sistema;
- validação do EPUB gerado com barra de progresso e avisos de capítulo vazio, link interno quebrado e imagem ausente;
- testes automatizados e CI no GitHub Actions.

Ainda não possui:

- biblioteca completa com importação automática, fila e histórico;
- gerenciamento automático do LibreTranslate;
- validação EPUB avançada com EPUBCheck;
- backends alternativos além de LibreTranslate;
- interface gráfica ou web.

## 3. Estrutura do projeto

Estrutura versionável principal:

```text
ayvu/
├── .github/
│   └── workflows/
│       └── tests.yml
├── docs/
│   ├── relatorio-tecnico.md
│   └── release-workflow.md
├── src/
│   └── ayvu/
│       ├── __init__.py
│       ├── cache.py
│       ├── chunking.py
│       ├── cli.py
│       ├── cli_progress.py
│       ├── config.py
│       ├── domain.py
│       ├── epub_io.py
│       ├── glossary.py
│       ├── html_translate.py
│       ├── library.py
│       ├── preflight.py
│       ├── review_export.py
│       ├── review_import.py
│       ├── resume.py
│       ├── translator.py
│       └── validation.py
├── tests/
│   ├── conftest.py
│   ├── test_cache.py
│   ├── test_chunking.py
│   ├── test_cli.py
│   ├── test_cli_progress.py
│   ├── test_config.py
│   ├── test_epub_io.py
│   ├── test_glossary.py
│   ├── test_html_translate.py
│   ├── test_library.py
│   ├── test_preflight.py
│   ├── test_review_export.py
│   ├── test_review_import.py
│   ├── test_resume.py
│   ├── test_translator.py
│   └── test_validation.py
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── glossary.example.json
├── pyproject.toml
└── uv.lock
```

Arquivos locais e privados ficam fora do Git, incluindo EPUBs, PDFs, caches SQLite locais e glossários pessoais.

## 4. Stack

Dependências principais:

- Python 3.11+;
- `typer` para CLI;
- `rich` para saída de terminal;
- `ebooklib` para inspeção de EPUB;
- `beautifulsoup4` e `lxml` para HTML/XML;
- `requests` para HTTP;
- `sqlite3` da biblioteca padrão para cache;
- `pytest` para testes.

O gerenciador preferido do projeto é `uv`.

## 5. Instalação e validação local

Instalar dependências de desenvolvimento:

```bash
uv sync --extra dev
```

Ver ajuda da CLI:

```bash
uv run ayvu --help
```

Rodar testes:

```bash
uv run pytest
```

Para mudanças apenas de documentação, a validação mínima é:

```bash
git diff --check
```

## 6. Comandos implementados

Inspecionar um EPUB:

```bash
uv run ayvu inspect livro.epub
```

Testar o LibreTranslate local:

```bash
uv run ayvu test-translator --url http://localhost:5000
```

Listar idiomas disponíveis no LibreTranslate:

```bash
uv run ayvu languages --url http://localhost:5000
```

Traduzir um EPUB:

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --translator libretranslate \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite
```

Exportar CSV para revisão externa durante a tradução:

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --review-output livro-review.csv
```

Importar o CSV revisado e reconstruir o EPUB sem alterar o original:

```bash
uv run ayvu apply-review livro.epub livro-review.csv \
  --output livro-revisado.epub
```

Gerar preview traduzido:

```bash
uv run ayvu --preview livro.epub
```

Extrair texto visível para Markdown:

```bash
uv run ayvu extract livro.epub --output livro-extraido/
```

Executar o menu guiado:

```bash
uv run ayvu
```

O modo guiado permite iniciar tradução, gerar preview, ver ajuda, acessar configurações e abrir a biblioteca inicial. Também pode detectar estados locais de tradução interrompida. Na tradução e no preview guiados, o idioma padrão salvo aparece como primeira opção; ao escolher `Outro idioma`, o Ayvu lista idiomas do LibreTranslate com nome, código e estado. A biblioteca lista livros das pastas configuradas de originais e traduções, mostra versões disponíveis e abre o EPUB escolhido no leitor configurado ou no leitor padrão detectado no sistema.

## 7. Modos de uso

O Ayvu separa dois perfis:

- `common`: modo comum, guiado, com confirmações e perguntas antes de ações importantes.
- `developer`: modo direto, adequado para automação e uso explícito por subcomandos.

Ao executar apenas `uv run ayvu`, o modo comum abre o menu guiado. Ao usar subcomandos como `translate`, `inspect` e `extract`, o comportamento padrão é mais direto.

A opção global `--mode` permite forçar o modo:

```bash
uv run ayvu --mode common translate livro.epub
```

## 8. Responsabilidades dos módulos

`src/ayvu/cli.py` concentra comandos Typer, argumentos, prompts, confirmações, progresso, relatórios e orquestração. Não deve receber regra pesada de EPUB, HTML ou HTTP.

`src/ayvu/domain.py` guarda tipos de domínio compartilhados, como `LanguagePair`, `OutputPlan`, `TranslationOptions` e `UserMode`.

`src/ayvu/epub_io.py` cuida de leitura, inspeção, extração, tradução estrutural e escrita do EPUB final.

`src/ayvu/html_translate.py` traduz HTML/XHTML por bloco (parágrafos, títulos e itens de lista), substituindo tags inline por placeholders neutros e restaurando-as depois, preservando tags e atributos e ignorando conteúdo que não deve ser traduzido.

`src/ayvu/review_export.py` escreve o CSV opcional de revisão externa com segmentos originais/traduzidos, IDs e metadados de rastreio, e centraliza o contrato de `segment_id` (`format_segment_id`/`parse_segment_id`).

`src/ayvu/review_import.py` lê o CSV de revisão, valida o cabeçalho, expõe as linhas e detecta `segment_id` duplicados. O comando `apply-review` usa `epub_io.apply_reviewed_epub` para reconstruir um novo EPUB a partir do original, casando cada segmento por documento e índice, validando o texto original e relatando ausentes, inconsistentes, duplicados e documentos desconhecidos.

`src/ayvu/library.py` varre as pastas de biblioteca configuradas, agrupa EPUB original e traduções por livro e resolve o comando usado para abrir EPUBs no app leitor.

`src/ayvu/translator.py` define o contrato `Translator` e o backend `LibreTranslateTranslator`.

`src/ayvu/cache.py` persiste traduções em SQLite usando idioma de origem, idioma de destino e hash do texto original. Também centraliza inspeção, limpeza filtrada, exportação JSON e importação JSON do cache.

`src/ayvu/glossary.py` lê glossários JSON no formato simples e no formato com
regras explícitas `translate`, `preserve` e `forbidden`.

`src/ayvu/chunking.py` divide textos longos preservando ordem e evitando cortar palavras quando possível.

`src/ayvu/preflight.py` valida idioma, glossário, cache, EPUB e tradutor antes da tradução real.

`src/ayvu/resume.py` registra e lê estados locais de retomada em `~/Documentos/Livros/Processando`.

`src/ayvu/cli_progress.py` adapta callbacks de tradução para `rich.Progress` e mantém contadores de textos.

`src/ayvu/config.py` define o formato inicial de configuração JSON, perfis de tradução, o caminho padrão do arquivo, leitura/gravação e resolução das pastas locais do Ayvu. O menu guiado de `Settings` permite alterar idioma padrão, pasta base, nomes das pastas das funcionalidades e app leitor, além de mostrar os perfis configurados.

`src/ayvu/validation.py` valida o EPUB gerado: confirma abertura e documentos XHTML/HTML e avisa sobre capítulos vazios, links internos quebrados e imagens referenciadas ausentes, com callback de progresso opcional. Qualquer aviso faz a execução falhar com código 1.

## 9. Configuração local

O formato inicial da configuração local fica em:

```text
$XDG_CONFIG_HOME/ayvu/config.json
```

Quando `XDG_CONFIG_HOME` não estiver definido, o fallback é:

```text
~/.config/ayvu/config.json
```

Formato versionado atual:

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

A precedência para campos cobertos por perfis de tradução é:

```text
argumentos da CLI > perfil selecionado > arquivo de configuração > padrões internos
```

Hoje a configuração já alimenta os diretórios padrão de preview, traduções, relatórios Markdown, estados de processamento e biblioteca. O app leitor configurado é usado pela biblioteca ao abrir EPUBs.
Perfis podem definir `target_language`, `glossary` e `style`; caminhos relativos de glossário são resolvidos a partir da pasta local `glossaries`. Com LibreTranslate, `style` é metadado informativo e não altera a chamada HTTP.

## 10. Pipeline de EPUB

A decisão mais importante do pipeline é não reconstruir o EPUB inteiro com `ebooklib.write_epub()`.

O fluxo atual é conservador:

```text
abrir EPUB original como ZIP
-> localizar documentos XHTML/HTML com ebooklib e caminhos internos reais
-> traduzir somente os documentos elegíveis
-> copiar todas as entradas originais para o novo EPUB
-> substituir apenas os documentos traduzidos
-> manter mimetype sem compressão
```

Essa abordagem preserva:

- `mimetype`;
- `content.opf`;
- `toc.ncx` e arquivos de navegação;
- CSS;
- imagens;
- links internos;
- nomes de arquivos internos;
- entradas não modificadas do ZIP.

## 11. Tradução de HTML

A regra principal é:

```text
Não achatar HTML.
Não usar get_text() no fluxo de tradução.
Traduzir somente texto visível ao leitor.
```

Tags ignoradas:

```text
script, style, code, pre, kbd, samp, svg, math
```

Também são ignorados comentários, `DOCTYPE`, declarações XML e processing instructions.

A tradução acontece por bloco. Sequências contíguas de texto e tags inline formam uma unidade de tradução, como parágrafos, títulos e itens de lista. As tags inline, como `em`, `strong` e links, viram placeholders neutros antes de enviar ao tradutor e são restauradas depois, preservando atributos e estrutura. Tags inline ignoradas (`code`, `kbd`, `samp`) e elementos vazios (como `br`) entram como placeholder opaco e não são traduzidos. Texto solto entre elementos de bloco continua sendo traduzido, sem perda.

Exemplo:

```html
<p>Any programming book with <em>Patterns</em> in its name.</p>
```

O parágrafo inteiro é traduzido como uma unidade, no formato:

```text
Any programming book with __AYVU_TAG_0__Patterns__AYVU_TAG_1__ in its name.
```

onde `__AYVU_TAG_0__` e `__AYVU_TAG_1__` representam `<em>` e `</em>`. Depois da tradução, os placeholders voltam a ser as tags originais. Isso dá mais contexto ao tradutor sem enviar HTML real e sem perder estrutura.

## 12. Cache, glossário e chunking

O cache SQLite usa chave baseada em:

```text
source_lang + target_lang + SHA-256(texto original)
```

Com a tradução por bloco, o "texto original" é o bloco inteiro com placeholders de tags, então a unidade do cache é o bloco, não mais o nó de texto isolado. Blocos sem tags mantêm a mesma chave de antes; blocos com tags geram chaves novas, o que evita reaproveitar por engano entradas antigas guardadas por nó.

O comando `ayvu cache` mantém o gerenciamento separado da tradução normal. Ele
permite inspecionar entradas por par de idiomas e datas, limpar entradas com
filtros ou `--all`, exportar JSON legível e importar o JSON exportado. A
exportação contém texto original e traduzido, então deve ser tratada como dado
privado.

O glossário é aplicado depois da tradução e também sobre textos recuperados do cache. Isso mantém o comportamento consistente entre texto novo e texto reaproveitado.

O formato simples de glossário (`"Termo": "tradução"`) continua sendo aceito e
equivale à regra `translate`. O formato avançado aceita objetos por termo com
`rule: "translate"` e `translation`, `rule: "preserve"` ou
`rule: "forbidden"`. As regras `translate` e `preserve` alteram o texto final e
podem usar `required: true` para exigir que o termo esperado apareça na saída. A
regra `forbidden` detecta termos que não devem aparecer no texto final.

O relatório de tradução acumula estatísticas de glossário por texto, capítulo e
EPUB. Ele contabiliza aplicações de `translate` e `preserve`, inclusive em textos
vindos do cache, e avisa termos obrigatórios ausentes ou termos proibidos
encontrados na saída.

### Decisão sobre múltiplos glossários

A decisão atual é manter um glossário ativo por tradução ou perfil e não aceitar
empilhamento genérico de vários arquivos `--glossary` na mesma execução.

Motivos:

- dois arquivos podem definir regras incompatíveis para o mesmo termo;
- a ordem de prioridade teria de virar contrato de usuário e de cache;
- o relatório precisaria explicar qual arquivo aplicou, bloqueou ou sobrescreveu
  cada termo;
- o modo comum ficaria mais complexo para um caso ainda não validado por uso real.

Quando o usuário precisar combinar termos gerais, técnicos e específicos de um
livro, a solução atual é compor um único glossário avançado e associá-lo ao livro
ou ao perfil de tradução.

A alternativa futura preferida não é empilhamento arbitrário, mas glossários por
papel. Um arquivo poderia declarar uma regra padrão no início, como `translate`,
`preserve` ou `forbidden`, e todos os termos internos herdariam essa regra. Isso
reduz repetição para glossários grandes e mantém uma separação natural entre
traduções preferidas, termos preservados e termos proibidos. Antes de implementar,
o Ayvu ainda precisa definir formato, precedência, detecção de conflito e impacto
no relatório.

Antes de enviar texto ao tradutor, `src/ayvu/html_translate.py` protege termos especiais com placeholders internos e os restaura depois da chamada HTTP. O escopo protegido inclui URLs, caminhos de arquivo, comandos de terminal, versões como `v1.2.0`, placeholders, código inline e identificadores técnicos simples. A tradução restaurada é gravada no cache antes da aplicação do glossário.

O Ayvu verifica a sequência e a quantidade dos marcadores antes de restaurá-los.
O reconhecimento de termos técnicos também considera as fronteiras junto a tags
inline, para preservar siglas e identificadores dentro de itálico ou links.
Se o serviço alterar, perder, duplicar ou reordenar um marcador, a resposta é
descartada e os trechos ao redor dos termos protegidos são traduzidos separadamente.
Nessa recuperação, os termos e a marcação HTML são recolocados localmente, sem
ser enviados ao tradutor. Limites de chunk que cortariam marcadores também usam
essa recuperação. O fallback faz no máximo 64 chamadas ao tradutor por bloco; se
esse limite for excedido, a recuperação falha. Isso pode aumentar as chamadas e
reduzir o contexto disponível ao traduzir cada trecho.

Traduções do cache e da memória são verificadas antes de serem reutilizadas:
resíduos reconhecíveis de marcadores, perda de termos protegidos ou alteração da
sequência das tags impedem o reaproveitamento. Na tradução normal, uma entrada
inválida do cache é substituída apenas depois de uma nova tradução válida. Em
`cache_only`, o trecho é tratado como ausente; em `dry_run`, não há reparo do cache.
Se a recuperação também falhar, o bloco original é preservado e o erro é
registrado (ou propagado com `fail_fast`), sem salvar a resposta inválida.
EPUBs já gerados precisam ser gerados novamente a partir do original para
receber essa correção.

Textos longos são divididos antes de serem enviados ao tradutor. A regra atual tenta dividir por:

```text
parágrafos
-> frases
-> palavras
-> corte inevitável de tokens muito grandes
```

O limite padrão é `3000` caracteres.

## 13. Preflight e erros esperados

Antes de uma tradução real, o Ayvu verifica:

- par de idiomas;
- glossário;
- criação do tradutor;
- escrita no cache;
- leitura do EPUB;
- chamada de teste ao tradutor.

Em `--dry-run`, a chamada real ao tradutor é pulada. Falhas esperadas são convertidas em mensagens curtas com causa provável e próximo passo, evitando traceback para erro comum de usuário.

## 14. Retomada local

Além do cache SQLite, traduções reais registram um estado local em:

```text
~/Documentos/Livros/Processando
```

Esse estado guarda caminhos e opções da execução para facilitar retomada pelo modo comum. Ele não substitui o cache e não é apagado automaticamente.

O cache continua sendo a parte que evita retraduzir textos já concluídos.

## 15. Relatórios

Ao final da tradução, o Ayvu mostra um relatório no terminal com:

- EPUB original;
- idiomas;
- saída gerada;
- arquivo de revisão, quando `--review-output` for usado;
- capítulos processados;
- textos traduzidos;
- textos reaproveitados do cache;
- textos pulados no dry-run;
- erros;
- avisos de validação (capítulo vazio, link interno quebrado, imagem ausente).
- quando houver glossário, termos configurados, aplicações, obrigatórios
  ausentes e termos proibidos encontrados.

A validação roda antes do relatório final, então os avisos aparecem na tabela do terminal e, no modo comum, também no relatório Markdown. Qualquer aviso faz a execução terminar com código 1.

Quando `--review-output` é informado, a CLI coleta os segmentos emitidos pelo fluxo normal de tradução e grava um CSV lado a lado. Cada linha inclui `segment_id`, caminho interno do documento no EPUB, índice de capítulo, idiomas, indicação de cache, texto original e texto traduzido. O CSV não é gerado em `--dry-run` e não sobrescreve arquivo existente sem `--overwrite`.

O comando `apply-review` fecha o ciclo: percorre o EPUB original com a mesma lógica de runs da tradução, casa cada segmento ao CSV por `document_path` e índice por documento, valida que o texto original ainda corresponde e aplica as traduções revisadas em um novo EPUB, preservando o original. Segmentos sem revisão ficam no idioma de origem; ausentes, inconsistentes, duplicados e documentos desconhecidos entram no relatório. Como o CSV guarda só o texto visível, formatação inline dentro de um parágrafo revisado vira texto plano, mantendo a estrutura de blocos do EPUB.

Quando vários EPUBs são informados no comando `translate`, a CLI entra em modo
batch e chama o mesmo fluxo de tradução para cada livro. A saída de cada item é
calculada em `--output-dir` ou na pasta de traduzidos configurada, usando o
padrão `<nome>-<idioma>.epub`. `--output` e `--review-output` ficam restritos ao
fluxo de um único EPUB para evitar caminhos ambíguos. O batch para no primeiro
erro por padrão; com `--continue-on-error`, registra a falha, processa os itens
restantes e termina com código 1 se qualquer livro falhar. Cada item salva um
relatório Markdown próprio na pasta de relatórios configurada.

O comando `translate` também aceita `--chapters` para tradução parcial. A CLI
parseia a expressão e mostra a tabela `Selected chapters`, mas a resolução real
fica em `epub_io`: os documentos traduzíveis são enumerados na ordem interna do
EPUB, a seleção casa índices 1-based, faixas ou padrões por título/nome/caminho,
e somente documentos selecionados entram em `translate_html`. Como a cópia final
do EPUB substitui apenas documentos traduzidos, capítulos fora da seleção são
preservados com o conteúdo original.

No modo comum, o Ayvu também oferece salvar esse relatório em Markdown em `~/Documentos/Livros/Relatorios`, sem sobrescrever relatórios anteriores. O relatório Markdown repete o resumo de glossário e inclui seções com termos aplicados e avisos quando existirem.

## 16. Bug crítico: EPUB com tela branca

Durante o desenvolvimento inicial, um EPUB traduzido abria no leitor, mas mostrava tela branca.

O EPUB original tinha capítulos internos com milhares de bytes, mas o EPUB gerado tinha documentos reduzidos a algo como:

```html
<head/>
<body/>
```

O problema não estava na tradução isolada do HTML. A função de tradução preservava o conteúdo quando testada separadamente.

O problema aparecia ao reescrever o livro com `ebooklib.write_epub()`, que reconstruía alguns documentos `EpubHtml` vazios.

A correção foi abandonar a reescrita completa pelo `ebooklib` e copiar o EPUB original como ZIP, substituindo somente os documentos traduzidos. Essa decisão continua sendo central para a estabilidade do Ayvu.

## 17. LibreTranslate

O backend atual é `LibreTranslateTranslator`.

Endpoint de tradução:

```text
http://localhost:5000/translate
```

Endpoint de idiomas:

```text
http://localhost:5000/languages
```

Subir LibreTranslate com Docker:

```bash
docker run -it -p 5000:5000 libretranslate/libretranslate
```

Testar conexão:

```bash
uv run ayvu test-translator --url http://localhost:5000
```

Se o servidor estiver indisponível, o Ayvu deve falhar com uma mensagem orientada a ação, não com traceback bruto.

## 18. Testes e CI

A suíte atual tem 189 testes definidos em `tests/`, cobrindo:

- cache SQLite;
- chunking;
- glossário;
- configuração local;
- tradução de HTML;
- tradução por bloco com placeholders de tags;
- preservação de tags;
- extração de texto visível;
- caminhos internos de EPUB;
- cópia conservadora do EPUB;
- validação do EPUB gerado;
- backend LibreTranslate;
- listagem de idiomas;
- preflight;
- estado de retomada;
- progresso;
- comandos CLI e fluxos guiados.

O CI está em `.github/workflows/tests.yml` e roda:

```bash
uv sync --extra dev --frozen
uv run pytest
```

## 19. Próximos passos técnicos

Prioridades que ainda fazem sentido:

1. Avaliar glossários por regra, com regra padrão no arquivo para `translate`,
   `preserve` ou `forbidden`, sem empilhamento genérico.
2. Melhorar validação pós-tradução, incluindo links, capítulos vazios, imagens ausentes e texto não traduzido.
3. Criar configuração persistente para idioma padrão, pastas e preferências.
4. Avaliar escopo de cache por backend, modelo, glossário ou perfil quando esses fatores passarem a alterar o resultado esperado.
5. Adicionar modo `--cache-only`.
6. Suportar backends alternativos.
7. Documentar arquitetura em um documento dedicado.
8. Preparar empacotamento e releases públicas.

## 20. Possível suporte a PDF

PDF continua sendo um alvo futuro e mais difícil que EPUB, porque não é uma estrutura semântica de livro. PDF é mais próximo de páginas impressas com posições absolutas.

Caminho mais realista:

```text
PDF
-> extrair blocos de texto
-> gerar EPUB reflowable
-> usar pipeline atual de tradução EPUB
```

Não é recomendado começar tentando traduzir PDF preservando layout perfeito. A tradução muda tamanho de texto e pode quebrar caixas, colunas, tabelas e fontes.

## 21. Ideia central

O Ayvu deixou de ser um script de tradução e virou uma base real de CLI:

```text
CLI instalável
EPUB original preservado
cache
glossário
tradução local
preflight
progresso visual
retomada
validação
testes
CI
documentação
```

O próximo salto é melhorar qualidade de tradução, robustez de validação e experiência de configuração sem comprometer a regra principal: nunca alterar o EPUB original.
