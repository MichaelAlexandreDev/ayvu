# Changelog

Todas as mudancas relevantes deste projeto serao registradas neste arquivo.

O formato segue a ideia de uma secao "Nao publicado" para mudancas ainda nao
lancadas e secoes versionadas quando uma release for publicada.

## Nao publicado

### Adicionado

- REF-18 (#286): aposentadoria do v1 aceita pelo dono: retraduzir os dados antigos e guardar os
  originais v1 como histórico; exigir modelo declarado; manter preview, dry run e workers.

- REF-18 (#286): proposta de aposentadoria do v1 com inventário, lacunas do runtime rico,
  dependências e alternativas para caches e retomadas antigas; nada é removido nesta entrega.

- Retomada rica reaproveita resultados brutos (#242): uma continuação usa os documentos que
  o job anterior concluiu, sem chamar tradutor nem cache, quando as nove dimensões
  registradas são iguais. Mudança em uma delas refaz o documento e avisa qual foi
  (`raw_results_changed_<dimensão>`); dimensão não registrada impede o reuso sem ser
  chamada de mudança. O job anterior não é reaberto. Os resultados brutos passam à versão 2.

- Recuperação conservadora de memória governada no runtime rich/v2 por
  `--translation-memory` (#162), após o cache exato v2. Concordância e fuzzy têm
  limites de candidatos/bytes e decisões com proveniência. Fuzzy sugere por padrão;
  `--tm-conservative-auto-apply --tm-apply-threshold 0.98` autoriza a política
  conservadora. `--tm-legacy-cache-compatibility` habilita separadamente candidatos
  v2 de máquina, sempre não revisados; v1 sem estágio é excluído. Valores finais
  aprovados com pins compatíveis não recebem glossário novamente. O fluxo sem
  rótulo e o esquema/lifecycle governado existente permanecem; a migração 3 da
  memória acrescenta apenas índices de busca.

- Pipeline rico de produção opcional por `translator_models.libretranslate` no
  `config.json`, com modelo `declared:<rótulo>`, `DocumentTranslator` e cache exato
  em arquivo irmão `<--cache>.v2.sqlite` (#252). O v1 fica intacto, sem promoção de
  entradas. Trocar o modelo no servidor exige trocar o rótulo para evitar reutilização
  de resultados antigos. Sem rótulo, dry run e preview conservam seus fluxos anteriores.
- No runtime rico, falhas de provedor interrompem o livro sem publicar EPUB parcial,
  preservando segmentos no cache para `resume`. Retomadas entre `legacy/v1` e
  `rich/v2` são recusadas. Esta integração exige `--workers 1` e recusa
  cache-only (incluindo `--require-full-cache`) antes do provedor.

- `--network-policy none|loopback` por execução, incluindo menus, idiomas, teste,
  tradução/lote, preview e retomada. O padrão é HTTP loopback; `none` recusa antes
  de DNS, sockets, sessão e payload. A escolha não é persistida (#159).

- Contrato de perfis de tradução v1 por projeto, migração explícita dos perfis
  globais, revisões imutáveis e snapshots persistentes. A projeção separa efeitos
  no provedor bruto, pós-processamento, operação e metadados inertes para futuros
  consumidores. A CLI, o pedido LibreTranslate e o cache de produção continuam
  usando seus contratos atuais; `style` permanece informativo.

### Alterado

- REF-17 (#285), PR D: root desktop com edição explícita e superfície projetada antes dos recursos;
  testes auditam receitas atuais de provedores e formatos, preservando a CLI e o launcher stub.

- REF-17 (#285), PR C: CLI e menu montados pela superfície projetada, preservando comandos, opções e ajuda do `ayvu` instalado.

- Os adaptadores locais de revisão saem de `composition/builders/review_*.py` para `review/local_*.py`;
  o builder de revisão só os conecta (D5, decisão do dono de 2026-11-12).

- REF-17 (#285), PR B: manifestos imutáveis de cinco edições e projeção pura de comandos, menu e bindings, sem alterar a CLI instalada.

- REF-07 (#275): os casos de uso de inspeção e exportação do texto visível vão para
  `formats/use_cases.py` e são exportados por `ayvu.formats`; acaba o ciclo `content ↔ formats`.

- REF-20 (#288), lote D: índice `docs/architecture/README.md`; a matriz de casos de uso vira o contrato
  `modules/interfaces-cli.md` e o snapshot `cross-cutting/cli-surface.md`; a verificação de dependências e o baseline
  da refatoração vão para `cross-cutting/`. `docs/architecture/` passa a ter só `decisions/`, `modules/` e
  `cross-cutting/`, todos os documentos em inglês, com até 400 linhas e links verificados.

- REF-20 (#288), lote C6: as decisões de fronteiras do monólito modular e do modelo de ameaças ficam em
  `decisions/`, e seus contratos vão para `cross-cutting/` (fronteiras, donos dos módulos, modelo de ameaças e
  registro de ameaças), junto com a política de rede e o guia de onde vai o código novo.

- REF-20 (#288), lote C5: os ADRs de desktop, worker local e secret store vão para `decisions/`; o mapa de
  arquivos da decisão de estrutura de pacotes vai para `cross-cutting/refactoring-map.md`; as decisões de assistência
  e de direitos são divididas por assunto em registros companheiros com o mesmo cabeçalho, todos com até 400 linhas.

- REF-20 (#288), lote C4: registro do pipeline separado dos contratos de tradução,
  projeto e contexto por módulo, com links e leitores preservados.

- REF-20 (#288), lote C3: registros de conteúdo, formatos e providers separados dos contratos
  por módulo, com tabela de compatibilidade, links e leitores preservados.

- REF-20 (#288), lote C2: registros de projetos, jobs e proveniência separados dos contratos
  operacionais por módulo, com links e leitores preservados.

- REF-20 (#288), lote C1: contratos de preferências, conhecimento, biblioteca, revisão e perfis
  de projeto reunidos por módulo, com leitores e caminhos atualizados.

- REF-20 (#288), lote B2: propostas de assistência e governança de direitos/finalidades traduzidas
  para inglês, com leitor documental ajustado; regras e estado Proposed preservados, sem mover ou dividir.

- REF-20 (#288), lote B1: 16 documentos de módulos e fronteiras traduzidos para inglês, com links e
  leitor documental ajustados; decisões, contratos e exemplos preservados, sem mover ou fundir arquivos.

- REF-20 (#288), lote A: cabeçalhos das 21 decisões de arquitetura padronizados, com teste de formato e
  inventário; estados, datas e registros de aceite preservados, sem mover ou dividir documentos.

- REF-19 (#287): `docs/architecture/where-code-goes.md`, o guia de onde vai o código novo, com a tabela por
  módulo e o checklist de PR, e `scripts/arch_metrics.py` no checklist de release.

- REF-16 (#284): `check_tree`, o verificador do teste de fronteiras antigo, sai de 119 linhas para um
  coordenador de 15 que chama seis passos na mesma ordem. Com isso o ratchet não lista mais nenhum teste.

- REF-16 (#284), lote L: os testes de arquitetura, documentação e decisões vão para `tests/architecture/`, os das
  etapas de CI para `tests/ci/`, o empacotamento do desktop para `tests/interfaces/desktop/` e as pesquisas para
  `tests/research/`. Nenhum arquivo de teste fica acima de 400 linhas, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K6: os testes de `TranslateProject` saem de um arquivo de 1.250 linhas para oito em
  `tests/translation/` (gates do plano, destino, cancelamento, reconciliação, renovação do lease, portas,
  diagnósticos e continuação), preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K5: testes do preflight movidos para `tests/translation/`, divididos entre recursos locais e
  rotas do provedor, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K4: testes de recuperação movidos para `tests/recovery/` (e a escolha do estágio de retomada para
  `tests/translation/`), divididos pelo código que cobrem, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K3: testes de documentos e cache exato divididos
  por responsabilidade, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K2: testes de contexto e recuperação de memória divididos
  por responsabilidade, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote K1: testes de lote e contratos de tradução movidos
  para `tests/translation/`, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote J5: testes de workflow e integração de lote divididos
  por responsabilidade, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote J4: testes de CLI rica, composição e rótulos de modelo
  divididos por responsabilidade, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote J3: testes do comando de projeto da CLI divididos em sete
  arquivos por responsabilidade, preservando os mesmos testes e invariantes core.

- REF-16 (#284), lote J2: arquivo-base de testes da CLI dividido por responsabilidade,
  com valores no kernel e preservação dos 43 testes e invariantes core.

- REF-16 (#284), lote J1: cinco módulos restantes de testes da CLI movidos para
  `tests/interfaces/cli/`, preservando os 57 testes e a seleção core.

- REF-16 (#284), lote I: testes da tradução legada movidos para seus pacotes,
  com divisões por responsabilidade e preservação dos testes e invariantes core.

- REF-16 (#284), lote H: testes de conteúdo e formatos movidos para seus pacotes,
  com divisões por responsabilidade e preservação dos testes e invariantes core.

- REF-16 (#284), lote G2: testes de revisão movidos para `tests/review/`,
  com divisões de codec, mapeamento e aplicação e preservação dos testes.

- REF-16 (#284), lote G: testes de glossário e memória movidos para seus pacotes,
  com divisões por contrato e preservação dos testes e invariantes core.

- REF-16 (#284), lote F: testes de cache movidos para seus pacotes,
  com divisões por contrato e preservação dos testes e invariantes core.

- REF-16 (#284), lote E: testes de jobs, artefatos e proveniência movidos para seus pacotes,
  com divisões por contrato e preservação dos testes e invariantes core.

- REF-16 (#284), lote D: testes de projetos e biblioteca movidos para seus pacotes,
  com divisões de store e perfis e preservação dos testes e invariantes core.

- REF-16 (#284), lote C: testes de provedores e rede movidos para seus pacotes,
  com divisões por contrato e preservação dos testes e invariantes core.

- REF-16 (#284), lote B: testes de kernel e preferências movidos para seus pacotes,
  com migrações SQLite divididas por transações e publicação, preservando os testes.

- REF-16 (#284), lote A: suporte de testes extraído para pacotes regulares, com
  imports absolutos e os mesmos testes e invariantes core.

- Licença: o Ayvu deixa de ser MIT e passa a ser proprietário, com todos os direitos
  reservados a S.Ralt. Versões até o commit `975dbe7` continuam MIT para quem as
  recebeu. O pacote é marcado para não ser enviado ao PyPI por engano.

- O transporte LibreTranslate fixa os sockaddrs loopback validados, ignora
  proxies/credenciais ambientes e `.netrc` e recusa todo redirect. URLs remotas,
  HTTPS, authorities ambíguas e credenciais na URL agora retornam recusa tipada,
  sem migração ou fallback. Erros HTTP/conexão omitem URL e corpo de resposta.
  A biblioteca avisa que o leitor externo fica fora da garantia offline (#159).

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
  `docs/architecture/modules/review.md` e `docs/architecture/modules/review.md`.
- `translate`, a tradução guiada e cada livro de um lote agora rodam pelo caso de
  uso `TranslateProject`, como um job durável com um único resultado final. Prompts,
  relatórios, nomes de saída e códigos de saída não mudam, salvo o que está abaixo.
  Dry-run e preview continuam sem projeto. Veja
  `docs/architecture/modules/translation-project.md`.
- `resume` e a oferta do modo guiado agora retomam pelo job da tradução interrompida,
  com um novo job ligado a ele, sempre que o arquivo de estado corresponde a esse job.
  Os prompts não mudam. Um arquivo antigo sem job correspondente segue pelo caminho
  anterior. Passam a ser recusados: retomar uma tradução ainda viva em outro processo, e
  um livro alterado que um projeto registrou. Antes, ambos rodavam em silêncio. Um arquivo
  de estado que não é UTF-8 deixa de causar traceback. A retomada continua sendo uma
  reexecução completa sobre o cache, sem reaproveitar etapas. Veja
  `docs/architecture/modules/recovery.md`.
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

- Glossários gerenciados: duas criações simultâneas do mesmo nome em grafias
  diferentes (por exemplo `Café.json` e `CAFE` com acento combinante) podiam ambas
  vencer em overlayfs, usado pelo Docker, porque a listagem usava um descritor aberto
  antes da trava. A listagem agora usa um descritor aberto sob a trava. Achado pela
  etapa Docker da #261.
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

- Gate de testes core (#282): os testes de CLI que a REF-14 leva para `tests/interfaces/cli/` voltam a ser
  `core`, então as pull requests os rodam como rodavam dentro de `test_cli.py`.
- CLI dividido por grupo de comandos (REF-14, #282, primeira parte): a saída Rich compartilhada vai para
  `interfaces/cli/render.py`, os padrões e a política de rede da execução para `options.py`, e os
  comandos `inspect`/`extract` e `languages`/`test-translator` para `commands/content.py` e
  `commands/translator.py`. O `app.py` registra os comandos na mesma ordem de antes, então a ajuda não
  muda. Os testes desses comandos saem de `test_cli.py` para `tests/interfaces/cli/`.
- CLI dividido, segunda parte (REF-14, #282): os comandos `cache` (inspect, clean, export e import) e os
  auxiliares que só eles usam vão para `commands/cache.py`, e seus testes para `tests/interfaces/cli/`.
- CLI dividido, terceira parte (REF-14, #282): `apply-review` vai para `commands/review.py`, e a leitura
  da configuração salva e da pasta de processamento vai para `interfaces/cli/settings.py`. Todo o CLI
  passa a buscar a pasta de processamento em `settings._processing_dir`, num só lugar, e os testes
  substituem esse ponto, em vez de uma cópia por módulo.
- CLI dividido, quarta parte (REF-14, #282): o relatório da tradução vai para `interfaces/cli/report.py`,
  e o local do livro de saída e a decisão sobre arquivo existente vão para `interfaces/cli/output.py`.
- CLI dividido, quinta parte (REF-14, #282): o plano e as opções da tradução, a execução de um livro, o
  lote e o preview saem de `app.py` para módulos próprios (`plan`, `translate_options`, `run`,
  `run_messages`, `run_support`, `batch` e `preview`).
- CLI dividido, sexta parte (REF-14, #282): os comandos `translate` e `resume` vão para
  `interfaces/cli/commands`, com a retomada que o fluxo guiado também usa.
- CLI dividido, última parte (REF-14, #282): o fluxo guiado vai para `interfaces/cli/guided`, um módulo
  por tela. O `app.py` fica só com os apps Typer, o callback `main` e o registro dos comandos.
- `review/exchange.py` dividido (REF-15, #283, primeira família): o modelo do pacote, os marcadores, o
  codec CSV, a conferência com o contexto atual e a leitura e escrita em disco viram módulos próprios. A
  API de `ayvu.review` e os bytes do pacote não mudam.
- `knowledge/cache/identity.py` dividido (REF-15, #283, segunda família): valores, escopos, entradas,
  resolução cache-only e memória compatível viram módulos próprios. A projeção e o decoder ficam em
  `identity.py`; a API de `ayvu.knowledge.cache` e as identidades persistidas não mudam.
- `translation/project.py` dividido (REF-15, #283, terceira família): contratos, renovação do lease e
  execução ativa viram módulos próprios. Admissão e reconciliação ficam em `project.py`; a API de
  `ayvu.translation` e o fingerprint do plano não mudam.
- `translation/document.py` dividido (REF-15, #283, quarta família): contratos, resultados brutos,
  replay, proveniência e bindings viram módulos próprios. O tradutor e o estado da execução ficam em
  `document.py`; a API de `ayvu.translation` e os bytes persistidos não mudam.
- `knowledge/cache/admin_v2_store.py` dividido (REF-15, #283, quinta família): consultas SQLite,
  arquivos, exportação, registros e importação passam a módulos próprios; os três coordenadores
  longos delegam passos contíguos, preservando formatos, checkpoints e transações.
- `composition/builders/review.py` dividido (REF-15, #283, sexta família): arquivos, contexto,
  pacotes, início e operações passam a módulos irmãos; os dois coordenadores longos delegam
  passos contíguos, preservando validação, publicação, jobs e proveniência.
- Interfaces em `ayvu.interfaces` (REF-13, #281): o CLI vai para `interfaces.cli` (`app`, `progress`,
  `contracts`) e o desktop para `interfaces.desktop`, com `bootstrap` como entrada. Os comandos `ayvu` e
  `ayvu-desktop` continuam os mesmos e passam a apontar para `ayvu.interfaces.cli.app:app` e
  `ayvu.interfaces.desktop.bootstrap:main`. Com isso termina a sequência de movimentações:
  só `content_use_cases` continua solto na raiz, à espera da decisão sobre onde ele mora.
- Revisão em `ayvu.review` (REF-12, #280): `review.application`, `review.exchange`,
  `review.csv_export` e `review.csv_import`. Review ganha uma camada própria acima de translation,
  porque aplica revisões sobre o resultado da tradução e translation nunca importa review. Somem a
  última exceção entre módulos da mesma camada e os dois contratos que ela pedia.
- Providers e tradução em pacotes (REF-11, #279): `ayvu.providers` (`ports`, `capabilities`, `discovery`,
  `service`, `network`, `session` e `libretranslate`) e `ayvu.translation` (`plan`, `pipeline`, `engine`,
  `document`, `chunking`, `preflight`, `resume_reuse`, `project`, `batch`, `batch_output` e `context`).
  O cliente HTTP do LibreTranslate sai do v1 para `providers.libretranslate.client`, porque o adaptador
  rico também o usa. A porta dos provedores fica em `providers.ports`, já que translation importa
  providers. O preflight recebe a checagem do cache v1 da composição. Some `translation ↔ v1`.
- Formatos em `ayvu.formats` (REF-10, #278): `formats.contracts`, `formats.service`, `formats.local`,
  `formats.epub` (`archive`, `adapter`, `io`, `html_structure`, `validation`) e
  `formats.markdown.writer`. O `epub_io.py` foi dividido: a leitura do EPUB fica em `formats.epub.io`,
  e a tradução v1 do livro inteiro, com o CSV de revisão, vai para `v1/epub.py`, porque só o v1 a usa.
  Formats não importa mais review nem v1, e somem `formats ↔ review` e `formats ↔ v1`.
- Conhecimento em `ayvu.knowledge` (REF-09, #277): `knowledge.cache` (identidade, esquema,
  repositório, intercâmbio e administração), `knowledge.memory` (`fuzzy` e o subpacote `approved`) e
  `knowledge.glossary` (regras e catálogo). `memory_retrieval` vira `translation.stages`, porque é um
  estágio do pipeline. A memória de tradução passa a receber os candidatos por um protocolo, então
  knowledge não importa mais o cache v1. Somem `knowledge ↔ translation` e duas exceções.
- Jobs, artefatos e projetos em pacotes (REF-08, #276, primeira parte): `ayvu.jobs`, `ayvu.artifacts`,
  `ayvu.projects` e `ayvu.projects.profiles`. `artifacts.destination` deixa de importar formats: a
  composição passa o guarda de saída e a publicação do formato. Os adaptadores (stores SQLite e JSON,
  o esquema do projeto, a sessão HTTP, o acesso ao sistema de arquivos) passam a ser módulos públicos
  próprios, e o `__init__` de cada pacote exporta só o modelo e os casos de uso. Assim, importar o
  modelo não carrega mais SQLite nem HTTP, o que a REF-07 tinha passado a fazer em `provenance` e
  `preferences`. Um teste garante isso.
- Retomada em `ayvu.recovery` (REF-08, #276, segunda parte): `job_recovery` e `job_recovery_store`
  viram `recovery.service` e `recovery.store`. As regras puras de reaproveitamento de estágio vão
  para `resume_reuse`, porque o pipeline de tradução também as usa e fica abaixo de recovery.
  `preferences` e `provenance` ganham linhas próprias nas camadas, seguindo os imports reais:
  `provenance` lê o job que registra e por isso fica acima de jobs. Os ciclos caem de 11 para 6.
- Módulos folha em pacotes (REF-07, #275): `ayvu.network` (`policy`, `http`), `ayvu.preferences`
  (`model`, que junta `preferences` e `config_values`, além de `service`, `store` e `config_facade`),
  `ayvu.library`, `ayvu.provenance` e `ayvu.content` (`model` e `protection`, que era
  `translation_protection`). Cada pacote exporta só o que os outros módulos usam. `provenance`
  desce uma camada, então `preferences` o importa por regra e não mais por exceção.
  `content_use_cases` fica onde está até o dono decidir onde ele mora.
- Runtime v1 em quarentena (REF-06, #274): `legacy_translation`, `html_translate`, `translator`,
  `cache`, `cache_legacy_format`, `cache_legacy_stream`, `cache_admin_legacy_store`, `resume` e
  `legacy_resume` vão para `ayvu.v1`, que continua sendo o padrão sem rótulo de modelo. `v1` vira
  uma camada abaixo de `composition`, e um contrato impede as interfaces de importá-lo. Os módulos
  abaixo que ainda o importam ficam em exceções com a issue que remove cada uma. Os testes do v1
  vão para `tests/v1/`, e `CORE_TEST_FILES` passa a usar caminhos dentro de `tests/`.
- Composição fora dos módulos (REF-05, #273): os oito `*_composition.py` vão para
  `ayvu.composition.builders`, a raiz do CLI para `ayvu.composition.cli` e a do desktop
  para `ayvu.composition.desktop`. Cada raiz é um módulo público próprio, então o desktop
  não carrega a montagem do CLI. `CliProviderSession` vai para `ayvu.provider_session`,
  porque o preflight a usa, e os tipos `_Book*` saem de `cli_contracts` para a raiz do CLI.
  Nenhum módulo de capacidade importa um builder. `scripts/size_ratchet.py --move` leva as
  entradas de um arquivo movido sem mudar seus tamanhos.
- `cli_contracts.py` dividido (REF-04, #272, segunda parte): os erros de traduzir um livro
  (`PreflightError`, `PreflightPolicyError`, `BookRefused`, `TranslationDryRunCancelled`,
  `ProjectTranslationUnavailable`, `BatchOutputError`, `ResumeStateError`) vão para
  `ayvu.translation`, e `ProviderSetupError` para `ayvu.providers`. Os protocolos que só o
  CLI usa ficam com o CLI. Os ciclos entre a interface e jobs, providers e translation
  deixam de existir: 9 pares restam dos 12.
- Núcleo compartilhado `ayvu.kernel` (REF-04, #272, primeira parte): identificadores,
  validação de UTC, falhas e avisos tipados, o token de cancelamento e os valores de
  domínio (`domain.py`) saem de `jobs` e da raiz; o executor de migrações SQLite vira
  `ayvu.kernel.sqlite`. As classes mantêm os nomes, então os mesmos erros continuam sendo
  capturados. 36 dependências de outros módulos em `jobs` deixam de existir.
- Travas de arquitetura (REF-03, #271), no conjunto `core`: catraca de tamanho (arquivos
  até 400 linhas, funções até 80, com os maiores de hoje em `tests/size_ratchet.json`),
  `import-linter` com as camadas da decisão de estrutura de pacotes e as 46 violações de
  hoje, cada uma com a issue que a remove, e a checagem de API pública entre pacotes.
- Decisão da estrutura de pacotes (REF-02, #267), aceita pelo owner em 2026-11-10:
  `docs/architecture/decisions/package-structure.md`, com as decisões D1 a D15, a árvore alvo,
  o destino de cada arquivo atual, os ciclos e a issue que desfaz cada um. A D15 manda escrever
  em inglês o que for commitado; o registro foi traduzido sem mudar nenhuma decisão.
- Base da refatoração (REF-01, #266): `tests/test_persisted_identities.py` congela as
  tags de domínio, as versões e um digest de referência de cada identidade persistida
  (cache v1 e v2, plano, perfil, contexto, decisão de rede, memória aprovada, resultados
  brutos), independente de onde o código more. `scripts/arch_metrics.py` mede arquivos
  e funções grandes e os ciclos entre módulos; a medição inicial está em
  `docs/architecture/refactor-baseline.md`.
- Testes em três etapas (#261): `scripts/check.sh` na máquina local (com gancho
  `pre-push` opcional), `scripts/docker-tests.sh` em Ubuntu, Debian, Fedora e Arch,
  sem rede e sem arquivos privados, e um único job `ci-gate` no GitHub. Pull requests
  rodam os testes marcados `core` e o `main` roda a suite completa; disparo manual e
  tags `v*` cobrem Python 3.11 e 3.14. O Dependabot agrupa as atualizações de versão.
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
