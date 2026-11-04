# Changelog

Todas as mudancas relevantes deste projeto serao registradas neste arquivo.

O formato segue a ideia de uma secao "Nao publicado" para mudancas ainda nao
lancadas e secoes versionadas quando uma release for publicada.

## Nao publicado

### Adicionado

- Modo comum guiado ao executar `uv run ayvu`.
- Modo desenvolvedor direto e opcao global `--mode`.
- Preview traduzido com `uv run ayvu --preview livro.epub`.
- Comando `languages` para listar idiomas retornados pelo LibreTranslate.
- Selecao guiada de idioma de destino no modo comum, usando o idioma padrao
  salvo ou a lista retornada pelo LibreTranslate.
- Configuracao local em JSON com idioma padrao, pasta base dos livros, nomes
  das pastas das funcionalidades e app leitor de EPUB.
- Fluxo de primeiro uso para escolher idioma padrao, pasta base e nomes de
  pastas antes de salvar a configuracao.
- Menu `Settings` para alterar idioma padrao, pasta base, nomes de pastas e app
  leitor.
- Biblioteca inicial para listar EPUBs originais e traduzidos e abrir o arquivo
  escolhido no leitor configurado ou detectado pelo sistema.
- Deteccao automatica do idioma de origem pelo metadado do EPUB quando
  `--source` nao e informado.
- Plano de traducao exibindo origem e destino antes de iniciar.
- Validacao de rota de traducao pelo LibreTranslate, com suporte a rota
  intermediaria via ingles quando nao houver rota direta.
- Preflight antes da traducao real, verificando EPUB, cache, glossario, idiomas,
  rota de traducao e tradutor.
- Comandos `cache inspect`, `cache clean`, `cache export` e `cache import` para
  gerenciar o cache SQLite fora do fluxo normal de traducao.
- Memoria de traducao opcional (`--translation-memory`) que reaproveita trechos
  parecidos por similaridade, com aplicacao acima de `--tm-apply-threshold` e
  sugestao de revisao entre `--tm-suggest-threshold` e `--tm-apply-threshold`.
- Controles de execucao do tradutor com `--requests-per-second`,
  `--retry-backoff` e `--retry-backoff-max`, incluindo retry de HTTP `429` e
  HTTP `5xx` com backoff exponencial.
- Opcao `--workers` no comando `translate` para processar documentos internos do
  EPUB em paralelo, mantendo `1` como padrao conservador, conexoes SQLite e
  sessoes HTTP separadas por worker e montagem final em ordem deterministica.
- Plano, progresso, interrupcao e retomada agora mostram os controles de
  execucao usados (`Workers`, `Rate limit` e `Retry policy`), incluindo a ordem
  deterministica do progresso quando `--workers` e maior que `1`.
- Perfis de performance com `--performance-profile low|standard|high|custom`,
  aplicando presets para workers, limite de requisicoes, chunking e politica de
  retry, com flags explicitas tendo precedencia.
- Estado local de retomada em `~/Documentos/Livros/Processando` ou na pasta de
  processamento configurada.
- Checkpoints de progresso no estado de retomada, atualizados a cada capitulo com
  total de capitulos, capitulo atual, capitulos concluidos, capitulos com falha e
  contagem de segmentos que falharam.
- Comando `resume` para retomar uma traducao em andamento a partir do checkpoint
  salvo, reaproveitando as opcoes da execucao e o cache.
- Oferta de retomada no modo comum quando uma traducao em andamento e detectada.
- Relatorio Markdown opcional no modo comum.
- Validacao do EPUB gerado com barra de progresso, avisos de capitulos vazios,
  links internos quebrados e imagens referenciadas ausentes.
- Tutorial para modo comum, fluxo intermediario e modo desenvolvedor.
- Testes com EPUB minimo gerado por codigo.
- GitHub Actions para rodar `uv run pytest`.
- Documento de fluxo de issues, branches, pull requests e releases.
- Arquivos iniciais de manutencao do projeto: `LICENSE`, `CHANGELOG.md` e
  `CONTRIBUTING.md`.
- Glossario avancado com regras `translate`, `preserve` e `forbidden`,
  mantendo compatibilidade com o formato simples de pares de termos.
- Relatorio de uso do glossario com contagem de termos aplicados, termos
  obrigatorios ausentes e termos proibidos encontrados na saida.
- Menu guiado de glossarios no modo comum, com criacao, edicao, previa,
  validacao, salvamento local e selecao antes de traduzir.
- Perfis de traducao em `config.json`, com selecao por `--profile`, idioma de
  destino padrao, glossario associado e campo de estilo informativo.
- Opcao `--translate-metadata` para traduzir o titulo do EPUB e a navegacao
  EPUB 3 de forma explicita e conservadora.
- Opcao `--translate-alt-text` para traduzir o texto alternativo (`alt`) das
  imagens, preservando a imagem e demais atributos e contando os textos no
  relatorio. Leitura de texto dentro da imagem (OCR) fica fora do escopo.
- Opcao `--review-output` para gerar CSV opcional com textos originais e
  traduzidos lado a lado, IDs de capitulo/segmento e metadados de rastreio do
  EPUB para revisao humana externa.
- Comando `apply-review` para reconstruir um EPUB a partir de um CSV revisado,
  validando que cada segmento ainda corresponde ao EPUB original, aplicando as
  traducoes revisadas sem alterar o original e relatando trechos sem revisao,
  ausentes, inconsistentes, duplicados ou de documentos desconhecidos.
- Traducao em batch no comando `translate`, aceitando multiplos EPUBs,
  `--output-dir`, relatorios Markdown por livro e `--continue-on-error` para
  seguir processando apos falhas.
- Opcao `--chapters` no comando `translate` para traduzir apenas capitulos
  selecionados por indice, faixa ou padrao, preservando os demais capitulos sem
  mudancas.

### Atualizado

- Traducao de HTML agora ocorre por bloco (paragrafos, titulos e itens de
  lista), substituindo tags internas como `em`, `strong` e links por
  placeholders e restaurando-as depois. O cache passa a usar o bloco como
  unidade de traducao.
- Saida padrao da traducao no modo comum agora e confirmada antes de iniciar.
- Modo comum permite sobrescrever, escolher outro nome ou cancelar quando o EPUB
  de saida ja existe.
- Erros esperados mostram mensagens mais curtas no modo comum e detalhes
  tecnicos no modo desenvolvedor.
- Avisos da validacao final entram no relatorio do terminal e no relatorio
  Markdown.
- README e relatorio tecnico foram atualizados para refletir modos de uso,
  retomada, configuracao, biblioteca, validacao, CI e roadmap.
- `glossary.example.json` agora demonstra o formato avancado de glossario.
- Documentada a decisao de manter um glossario ativo por traducao ou perfil,
  adiando empilhamento generico e preferindo uma futura evolucao por glossarios
  com regra padrao de arquivo.

### Corrigido

- `--translate-metadata` e a aplicacao de uma revisao mudam so o texto do titulo no OPF.
  Atributos como `opf:role`, declaracoes de namespace, comentarios e CDATA nao se perdem
  mais, e o OPF fica identico byte a byte quando o titulo nao muda.
- Um titulo com comentario, elemento ou entidade dentro fica sem traducao e aparece como erro
  no relatorio, em vez de ser traduzido em parte.
- Uma traducao de titulo com caractere invalido em XML nao gera mais um OPF invalido: na
  traducao ela vira erro no relatorio, e ao aplicar uma revisao o comando falha sem gerar saida.
- Atributos com prefixo em tags inline, como `epub:type` e `xlink:href`, nao perdem mais
  o prefixo no EPUB traduzido.
- Comentarios e instrucoes de processamento dentro de tags inline nao viram mais texto
  visivel ao leitor.
- Texto do livro parecido com um marcador interno (`__AYVU_TAG_0__`) nao e mais trocado por
  marcacao.
- Interrupcao com `Ctrl+C` mostra progresso parcial e orienta reutilizar o
  cache.
- EPUB invalido em `inspect` e `extract` falha sem traceback.
- Texto duplicado no README foi removido.

### Interno

- Progresso de traducao foi extraido para `src/ayvu/cli_progress.py`.
- Responsabilidades do backend LibreTranslate foram separadas em classes e
  parsers menores.
- Erros estruturais de EPUB foram separados do relatorio de traducao.
- Formato de configuracao, estado de retomada e validacao final ganharam testes
  dedicados.

## 0.0.1 - Inicial

### Adicionado

- CLI `ayvu` para inspecionar, traduzir e extrair texto visivel de EPUBs
  locais.
- Backend HTTP inicial compativel com LibreTranslate.
- Cache SQLite para reaproveitar traducoes e retomar execucoes interrompidas.
- Glossario JSON opcional.
- Validacao basica do EPUB gerado.
- Relatorio final no terminal e opcao de salvar relatorio em Markdown.
