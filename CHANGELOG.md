# Changelog

Todas as mudancas relevantes deste projeto serao registradas neste arquivo.

O formato segue a ideia de uma secao "Nao publicado" para mudancas ainda nao
lancadas e secoes versionadas quando uma release for publicada.

## Nao publicado

### Adicionado

- Contrato de perfis de tradução v1 por projeto, migração explícita dos perfis
  globais, revisões imutáveis e snapshots persistentes. A projeção separa efeitos
  no provedor bruto, pós-processamento, operação e metadados inertes para futuros
  consumidores. A CLI, o pedido LibreTranslate e o cache de produção continuam
  usando seus contratos atuais; `style` permanece informativo.

### Alterado

- O cache standalone dispõe de preparação e repositório exato v2 para o caminho
  rico. A migração preserva as entradas v1 sem promovê-las a acertos v2. A CLI
  administra namespaces explícitos e oferece importação v2 com mapa externo e
  previsão; os comandos legados continuam disponíveis. Representações dos DTOs
  legados de cache ocultam texto e caminho.
- O catálogo guiado de glossários recusa links simbólicos e arquivos com múltiplos
  links físicos, sinaliza arquivos inválidos ou grandes demais e limita arquivos,
  entradas e texto. Salvar é atômico, e uma edição é recusada se o arquivo mudou
  depois de aberto. Caminhos explícitos `--glossary` mantêm o carregador avançado
  existente.

- O comando `translate` aceita no máximo 100 EPUBs e recusa `--missing-output`
  com múltiplos arquivos; em cache-only, o nome dos textos ausentes continua
  automático por livro. Destinos duplicados são recusados antes do lote, e as
  decisões de conflito do modo comum são coletadas antes do primeiro item.
  A exaustão dos nomes automáticos falha apenas o item. Uma interrupção retorna
  os resultados dos itens iniciados e não inicia o próximo; uma recusa anterior
  à execução não exibe resumo vazio. Falha ao salvar o relatório falha o item,
  preservando a informação de EPUB já publicado. Ainda há uma janela entre a
  última verificação e a substituição ou criação exclusiva do arquivo.
- A abertura de um EPUB da biblioteca revalida o arquivo antes de iniciar o leitor; um arquivo
  substituído pede para abrir a biblioteca de novo. Itens ignorados, como pasta sem permissão ou
  link para fora da pasta de livros, geram um aviso em vez de sumir em silêncio.
  Interpretadores de comandos como `sh` não são aceitos em `Reader app`.
- Inspecionar ou exportar cache ausente não cria o banco nem sua pasta; a
  exportação continua produzindo JSON vazio. A limpeza verifica se a seleção
  mudou antes de remover entradas e erros detalhados usam códigos sem conteúdo.
  A exportação recusa substituir o próprio arquivo SQLite do cache.
  Exportação e importação têm limite de 512 MiB e 1.000.000 de entradas; acima
  disso, exporte em partes com `--source`, `--target` ou `--before`.

### Adicionado

- Extra opcional `desktop` com PySide6-Essentials e ponto de entrada reservado
  `ayvu-desktop`; a interface será implementada em DESKTOP-010. A instalação
  da CLI continua independente do Qt.

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
- Opcao `--review-output` para exportar um pacote v2 vinculado ao projeto,
  à fonte, ao artefato traduzido e às unidades estáveis.
- Comando `apply-review` com validação opcional `--validate-only` e publicação
  reconciliável de um novo EPUB revisado, preservando a fonte e a tradução
  automática.
- Traducao em batch no comando `translate`, aceitando multiplos EPUBs,
  `--output-dir`, relatorios Markdown por livro e `--continue-on-error` para
  seguir processando apos falhas.
- Opcao `--chapters` no comando `translate` para traduzir apenas capitulos
  selecionados por indice, faixa ou padrao, preservando os demais capitulos sem
  mudancas.

### Atualizado

- `inspect` e `extract` agora usam os casos de uso compartilhados de inspeção e
  exportação de texto visível pelo registry de formatos. A tabela, os nomes e o
  Markdown continuam iguais; a inspeção é somente de leitura. `extract` valida
  todos os destinos antes de escrever e publica cada arquivo atomicamente,
  preservando arquivos sem relação. Se uma falha ocorrer após uma publicação, a
  saída informa o número concluído e o restante. No modo desenvolvedor, o detalhe
  que antes mostrava a exceção do parser agora mostra um código de motivo
  normalizado, sem conteúdo do livro ou caminhos originados pelo parser.
  `inspect` de livro protegido por DRM mostra só um aviso, sem os dados do livro, e
  `extract` o recusa, como `translate` já fazia. Em `extract`, caracteres de nome de
  capítulo inválidos em algum sistema (`c:h`, `c*h`) viram `_`, e nomes longos demais
  são encurtados, para o texto de todo capítulo ser exportado. Pasta de saída
  inválida gera mensagem própria em vez de falha não tratada.
- `apply-review` aplica só as linhas marcadas como `reviewed`; as demais mantêm a
  tradução automática e não são registradas como revisão humana. Editar uma linha sem
  marcá-la, ou marcar uma como `rejected`, recusa o pacote.
- O contrato de revisão ganhou o pacote v2 `ayvu.review_exchange`, limitado e
  ligado a projeto, fonte, artefato de tradução, formato, locator e unidades
  estáveis. CSV v1 agora é apenas para inspeção: `apply-review` recusa antes de
  abrir o EPUB ou criar saída e informa `legacy_reexport_required`. O caso de uso
  de aplicação agora exporta pacotes v2 de `TranslateProject`, valida sem efeitos
  colaterais e aplica pelo protocolo de publicação/reconciliação D106. `apply-review`
  aceita `--validate-only`; v1 continua exigindo reexportação. Veja
  `docs/architecture/review-exchange.md` e `docs/architecture/review-application.md`.
- `translate`, a tradução guiada e cada livro de um lote agora rodam pelo caso de
  uso `TranslateProject`, como um job durável com um único resultado final. Prompts,
  relatórios, nomes de saída e códigos de saída não mudam, salvo o que está abaixo.
  Dry-run e preview continuam sem projeto. Veja
  `docs/architecture/translate-project.md`.
- `resume` e a oferta do modo guiado agora retomam pelo job da tradução interrompida,
  com um novo job ligado a ele, sempre que o arquivo de estado corresponde a esse job.
  Os prompts não mudam. Um arquivo antigo sem job correspondente segue pelo caminho
  anterior. Passam a ser recusados: retomar uma tradução ainda viva em outro processo, e
  um livro alterado que um projeto registrou. Antes, ambos rodavam em silêncio. Um arquivo
  de estado que não é UTF-8 deixa de causar traceback. A retomada continua sendo uma
  reexecução completa sobre o cache, sem reaproveitar etapas. Veja
  `docs/architecture/job-recovery.md`.
- Passam a ser recusados, com mensagem clara e em todos os modos, EPUBs com conteúdo
  protegido, nomes internos inseguros, arquivos internos repetidos, entidades XML ou
  além dos limites de tamanho e compressão. Antes eles eram traduzidos.
- `mimetype` defeituoso agora é corrigido na saída.
- `--fail-fast` agora termina com uma mensagem, e não com um traceback.
- A saída não aparece mais durante a escrita: ela é publicada de uma vez no fim, e a
  validação roda antes disso. Um `--overwrite` só troca o arquivo antigo no fim.
- Um computador suspenso por mais de uma hora faz a tradução terminar com falha, sem
  publicar nada. O que foi traduzido fica no cache.
- A mesma tradução em dois processos ao mesmo tempo é recusada.

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

- `--overwrite` com a saída igual ao original agora é recusado antes de qualquer
  escrita. Antes o comando terminava com `BadZipFile`.

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
