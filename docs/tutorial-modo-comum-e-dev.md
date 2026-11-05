# Tutorial: modo comum e modo desenvolvedor

Este tutorial mostra os fluxos principais do Ayvu para dois públicos:

- usuários comuns, que preferem menus guiados e confirmações antes de ações importantes;
- usuários técnicos, que preferem comandos diretos, scripts e argumentos explícitos.

O Ayvu traduz arquivos EPUB locais usando um servidor HTTP compatível com LibreTranslate. Ele não altera o EPUB original; a tradução sempre é gravada em um novo arquivo `.epub`.

## Antes de começar

Com Python 3.11 ou mais recente e `uv`, instale a CLI no diretório do projeto:

```bash
uv sync
```

Os dois modos deste tutorial são modos da CLI. Para instalar o suporte
desktop opcional baseado em PySide6 e executar seu ponto de entrada:

```bash
uv sync --extra desktop
uv run --extra desktop ayvu-desktop
```

O ponto de entrada ainda não abre uma interface; a implementação será tratada
em DESKTOP-010. Para desenvolvimento, use `uv sync --extra
dev`; para incluir o desktop, acrescente `--extra desktop`. Ao executar comandos
com `uv run`, informe os extras necessários para manter essa seleção.

Suba um LibreTranslate local:

```bash
docker run -it -p 5000:5000 libretranslate/libretranslate
```

Teste a conexão:

```bash
uv run ayvu test-translator --url http://localhost:5000
```

Se quiser conferir os idiomas retornados pelo servidor:

```bash
uv run ayvu languages --url http://localhost:5000
```

## Tutorial básico: modo comum

Use o modo comum quando quiser que o Ayvu guie a execução no terminal.

Abra o menu inicial:

```bash
uv run ayvu
```

O menu permite iniciar uma tradução, gerar preview, abrir biblioteca, gerenciar glossários, ver ajuda e acessar configurações. A biblioteca lista EPUBs das pastas configuradas para originais e traduções, mostra as versões disponíveis e permite abrir o arquivo escolhido no leitor configurado ou no leitor padrão detectado no sistema. As configurações permitem alterar idioma padrão, pasta base dos livros, nomes das pastas das funcionalidades e app leitor de EPUB, além de mostrar os perfis de tradução configurados.

No primeiro uso, o Ayvu pergunta o idioma padrão de leitura/tradução e a pasta base dos livros. Em seguida, mostra os nomes das pastas das funcionalidades e permite manter os padrões ou alterá-los uma única vez antes de salvar a configuração. Essa pasta é usada para organizar biblioteca, previews, relatórios, traduções e estados de processamento.

Se houver perfis de tradução no arquivo de configuração, a tradução guiada mostra esses perfis antes da escolha do idioma de destino. Um perfil pode fornecer um idioma padrão diferente e um glossário associado.

### Gerar um preview

O preview é o primeiro teste recomendado para um livro novo. Ele traduz uma amostra inicial do EPUB e preserva a estrutura do restante do arquivo.

Pelo menu, escolha a opção de preview e informe o caminho do EPUB quando solicitado.

Também é possível chamar o preview diretamente:

```bash
uv run ayvu --preview livro.epub
```

Por padrão, o arquivo é salvo em:

```text
~/Documentos/Livros/Preview/livro-preview.epub
```

Abra o preview no seu leitor de EPUB e confira capa, sumário, primeiro capítulo, links e trechos com formatação.

### Traduzir o livro completo

Pelo menu inicial, escolha traduzir livro e informe o caminho do EPUB. O Ayvu mostra o idioma de destino padrão como primeira opção e também oferece `Outro idioma`. Ao escolher outro idioma, ele lista os idiomas do LibreTranslate com nome, código e estado. Se houver glossários salvos, o Ayvu permite escolher um deles antes de confirmar onde o arquivo traduzido será salvo.

Antes de iniciar uma tradução real, o Ayvu verifica EPUB, idioma, glossário, cache e tradutor. Se algo estiver errado, ele falha cedo com uma mensagem curta e um próximo passo.

Sem caminho de saída explícito, o padrão é:

```text
~/Documentos/Livros/Traduzidos/livro-pt.epub
```

Se esse EPUB de saída já existir, o modo comum permite sobrescrever, escolher outro nome
ou cancelar sem alterar o arquivo existente.

Ao final, o Ayvu mostra um relatório no terminal com capítulos processados, textos traduzidos, textos reaproveitados do cache, erros e caminho de saída. No modo comum, ele também pode salvar esse relatório em Markdown em:

```text
~/Documentos/Livros/Relatorios
```

Quando precisar revisar a tradução fora do EPUB, use o modo desenvolvedor com
`--review-output` para salvar um pacote CSV v2 sensível, vinculado ao projeto,
à fonte exata, à tradução automática e às unidades estáveis. O modo comum não
pede esse arquivo automaticamente.

### Retomar uma tradução interrompida

Durante traduções reais, o Ayvu registra um checkpoint local em:

```text
~/Documentos/Livros/Processando
```

Esse checkpoint (`*.ayvu-state.json`) é atualizado a cada capítulo e guarda o progresso: total de capítulos, capítulo atual, capítulos concluídos e capítulos com falha. Ele também preserva os controles de execução usados na tradução, como workers, limite de requisições e backoff.

Ao executar `uv run ayvu`, o modo comum procura estados em andamento, mostra o resumo do checkpoint e os controles salvos (`Workers`, `Rate limit` e `Retry policy`), e oferece retomar a tradução detectada. O cache SQLite continua sendo a parte que evita retraduzir textos já concluídos: na retomada, só os trechos ainda não cacheados (os que faltaram ou falharam) chamam o tradutor de novo.

## Tutorial intermediário: preview, glossário e organização

Use este fluxo quando estiver traduzindo livros técnicos ou quiser controlar melhor o resultado.

### Usar glossário

No modo comum, escolha `Glossaries` no menu inicial. O Ayvu salva glossários guiados em:

```text
$XDG_CONFIG_HOME/ayvu/glossaries
```

Se `XDG_CONFIG_HOME` não estiver definido, o fallback é:

```text
~/.config/ayvu/glossaries
```

Ao criar ou editar um glossário, informe o termo original e escolha entre uma tradução
preferida ou preservar o termo sem tradução. O Ayvu mostra uma prévia dos termos, valida o
JSON e salva o arquivo. Uma edição é recusada se o arquivo mudou depois de aberto. Na próxima tradução guiada, se houver glossários salvos, escolha o
glossário desejado antes de iniciar o processamento.

O catálogo guiado aceita no máximo 1.000 arquivos JSON. Cada arquivo e a saída
serializada podem ter até 1 MiB, com no máximo 10.000 entradas, termos de até
1.024 caracteres e traduções de até 4.096 caracteres. Arquivos inválidos, grandes
demais, links simbólicos e arquivos com múltiplos links físicos não podem ser
selecionados. Escolha outro ou corrija o arquivo fora do fluxo guiado. Nomes e conteúdo do glossário são dados privados. O caminho
explícito `--glossary` continua usando o carregador avançado existente, separado
dessas regras do catálogo gerenciado.

No modo desenvolvedor, crie um glossário a partir do exemplo versionado:

```bash
cp glossary.example.json glossary.json
```

Edite `glossary.json` com os termos que deseja padronizar. O formato simples
continua aceito:

```json
{
  "Game Loop": "loop de jogo",
  "Design Pattern": "padrão de projeto",
  "Observer": "Observer"
}
```

Para regras explícitas, use `translate`, `preserve` ou `forbidden` por termo:

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

Depois, passe o glossário na tradução:

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite \
  --glossary glossary.json
```

O glossário é aplicado depois da tradução e também sobre textos vindos do cache.
Use `required: true` em regras `translate` ou `preserve` quando o termo esperado
deve aparecer na saída. Ao final, o relatório conta termos aplicados e avisa
termos obrigatórios ausentes ou termos `forbidden` encontrados no texto final.

Use um glossário ativo por tradução ou perfil. O Ayvu não empilha vários arquivos de
glossário na mesma execução, porque isso exigiria resolver conflitos entre regras diferentes
para o mesmo termo. Quando um livro precisar de termos gerais, técnicos e específicos, componha
esses termos em um único glossário por enquanto.

Uma evolução futura pode separar glossários por papel, por exemplo um arquivo todo de
traduções preferidas, outro de termos preservados e outro de termos proibidos. Para isso, o
arquivo precisaria declarar sua regra padrão no início, em vez de repetir `rule` em cada termo.
Esse formato ainda não está implementado.

### Usar perfis de tradução

Perfis ficam no arquivo de configuração local e agrupam opções reutilizáveis:

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

Use o perfil no modo desenvolvedor:

```bash
uv run ayvu translate livro.epub --profile technical
```

No modo comum, escolha o perfil quando ele aparecer antes da seleção do idioma. Caminhos relativos
em `glossary` são resolvidos dentro de `$XDG_CONFIG_HOME/ayvu/glossaries` ou
`~/.config/ayvu/glossaries`. `--target` e `--glossary` continuam tendo precedência sobre o
perfil. O campo `style` aceita `neutral`, `technical`, `literary` ou `academic`, mas com
LibreTranslate ele ainda é apenas informativo.

### Usar cache de forma consistente

A CLI usa o cache legado v1. Os comandos de administração neste tutorial são
para v1; em v2, limpeza, importação e exportação retornam `v2-admin-required`,
e a inspeção pode recusar o namespace. Conserve o v1 somente para leitura e
escolha outro arquivo para um novo v2 pela API `prepare_cache`; acompanhe
DESKTOP-127 para administração v2. A migração não transforma entradas legadas
em acertos exatos v2. Veja [cache standalone v2](architecture/standalone-cache-v2.md).

Para retomar com segurança, repita o mesmo cache entre execuções:

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite
```

Se o processo for interrompido, rode o comando novamente com o mesmo cache. O Ayvu reaproveita os trechos já traduzidos.

Para ver o que existe no cache, use:

```bash
uv run ayvu cache inspect --cache .cache/traducoes.sqlite
```

Para limpar entradas antigas ou de um par de idiomas específico, faça primeiro
uma prévia:

```bash
uv run ayvu cache clean \
  --cache .cache/traducoes.sqlite \
  --source en \
  --target pt \
  --dry-run
```

Se o resultado estiver correto, repita com `--yes` no lugar de `--dry-run`.
Para limpar o cache inteiro, use `--all --yes`.

Também é possível exportar e importar o cache em JSON legível:

```bash
uv run ayvu cache export cache-ayvu.json --cache .cache/traducoes.sqlite
uv run ayvu cache import cache-ayvu.json --cache .cache/traducoes.sqlite
```

Esse JSON contém textos originais e traduzidos. Trate o arquivo exportado como
privado.

Se o cache ainda não existir, a inspeção mostra zero entradas e a exportação
gera um JSON vazio sem criar o cache ou sua pasta. Se a limpeza informar que a
seleção mudou, rode novamente a prévia e depois o comando com `--yes`.
Escolha para a exportação um arquivo diferente do cache SQLite; detalhes de
erros usam códigos curtos sem o conteúdo das entradas.
Exportação e importação têm limite de 512 MiB e 1.000.000 de entradas; acima
disso, exporte em partes com `--source`, `--target` ou `--before`.

### Reaproveitar trechos parecidos com a memória de tradução

O cache só reaproveita trechos idênticos. Para também reaproveitar trechos
**parecidos** (útil em livros técnicos e séries), ative a memória de tradução, que
fica desligada por padrão:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --cache .cache/traducoes.sqlite \
  --translation-memory
```

Ela usa os pares já guardados no cache. Acima de `--tm-apply-threshold` (padrão
`0.95`) a tradução parecida é reaproveitada direto; entre `--tm-suggest-threshold`
(padrão `0.80`) e o limiar de aplicação, o trecho é traduzido normalmente e a
correspondência fica registrada como sugestão de revisão no relatório.

Cuidado: trechos parecidos podem ter sentido diferente. Use limiares altos, mantenha
a memória desligada em livros novos e sensíveis, ou combine com `--review-output`
para conferir o resultado. A memória de tradução ainda não roda junto com
`--workers` maior que `1`; use uma das duas otimizações por execução.

### Ajustar configurações

No menu inicial, escolha `Settings` para ver os valores atuais e alterar preferências locais. A pasta base padrão inicial é:

```text
~/Documentos/Livros
```

Dentro dela, o Ayvu usa os nomes configurados para previews, traduções finais, relatórios, estados de processamento e biblioteca. Você também pode alterar o nome da pasta `Original` e configurar o app leitor de EPUB usado pela biblioteca.

### Usar biblioteca

A biblioteca inicial usa a estrutura configurada no modo comum. Com os nomes padrão:

```text
~/Documentos/Livros/
├── Original/
├── Preview/
├── Traduzidos/
├── Relatorios/
└── Processando/
```

Mantenha EPUBs originais em `Original`, previews em `Preview`, traduções finais em `Traduzidos` e relatórios em `Relatorios`. Ao escolher `Open library`, o Ayvu lista os livros em ordem alfabética, mostra quais têm original e quais traduções foram encontradas, permite ver informações do livro e abre a versão selecionada no leitor de EPUB.

Ao pedir a abertura, o arquivo é verificado novamente antes de iniciar o leitor. Links para fora
da pasta de livros não entram na biblioteca; itens ignorados geram um aviso.

Se o Ayvu não detectar um leitor padrão do sistema, abra `Settings` e preencha `Reader app` com o executável e suas opções, por exemplo `foliate --new-window`. Não use um interpretador de comandos como `sh`.

## Tutorial dev: comandos diretos

Use o modo desenvolvedor quando quiser execução previsível por terminal, automação ou scripts.

### Inspecionar um EPUB

```bash
uv run ayvu inspect livro.epub
```

`inspect` é somente de leitura e mantém a tabela com caminho, título, autores,
idioma, documentos e itens. Não cria cache ou projeto. Livro protegido por
DRM recebe só um aviso, sem título ou autores. `translate` e `extract` o recusam. Em modo desenvolvedor,
falhas mostram um código de motivo normalizado, sem repassar mensagens internas
do parser.

### Testar o tradutor

```bash
uv run ayvu test-translator --url http://localhost:5000
```

### Listar idiomas

```bash
uv run ayvu languages --url http://localhost:5000
```

### Gerar preview

```bash
uv run ayvu --preview livro.epub
```

### Traduzir com saída explícita

```bash
uv run ayvu translate livro.epub \
  --output livro-pt.epub \
  --source en \
  --target pt \
  --translator libretranslate \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite
```

### Traduzir título e sumário opcionalmente

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --translate-metadata
```

Sem `--translate-metadata`, o Ayvu preserva o título do OPF e o documento de
navegação. Com a opção ativa, traduz o primeiro `dc:title` e o texto do sumário
EPUB3 marcado como `properties="nav"`, mantendo identificadores, autores,
editora e metadados técnicos. Use com cuidado: alguns leitores e bibliotecas
usam o título e o sumário para organizar o livro.

No OPF, só o texto do título muda. Se o título não mudar, o OPF fica idêntico ao
original. Um título com comentário, elemento ou entidade dentro não é traduzido, e
isso aparece como erro no relatório.

### Traduzir texto alternativo de imagens opcionalmente

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --translate-alt-text
```

Sem `--translate-alt-text`, o Ayvu preserva o atributo `alt` das imagens. Com a
opção ativa, traduz apenas o `alt` das tags `<img>`, mantendo a imagem, o `src` e
os demais atributos; imagens decorativas (`alt=""`) são ignoradas. O relatório
mostra a quantidade de textos alternativos traduzidos. Ler o texto que está
dentro da imagem (OCR) continua fora do escopo.

### Traduzir vários EPUBs em batch

```bash
uv run ayvu translate livro-1.epub livro-2.epub livro-3.epub \
  --source en \
  --target pt \
  --output-dir traduzidos/ \
  --continue-on-error
```

O comando aceita até 100 EPUBs e os processa sequencialmente na ordem informada.
Sem `--source`, o idioma de origem é identificado nos metadados de cada livro;
os livros podem ter idiomas diferentes. Quando mais de um EPUB é informado, o Ayvu calcula uma saída por livro usando o
padrão `<nome>-<idioma>.epub` dentro de `--output-dir`. Sem `--output-dir`, usa a
pasta de traduzidos configurada. O batch não aceita `--output`,
`--review-output` nem `--missing-output`, pois essas opções apontam para um único arquivo.

Por padrão, o comando para no primeiro livro que falhar. Com
`--continue-on-error`, os próximos EPUBs continuam sendo processados, mas a
execução termina com código 1 se qualquer item falhar. Saídas existentes seguem
a mesma regra do fluxo individual: use `--overwrite` para substituir no modo
desenvolvedor, ou confirme a substituição quando estiver no modo comum. O Ayvu
recusa destinos de EPUB duplicados antes de começar e pede as escolhas para
todos os conflitos do modo comum antes de processar o primeiro livro.

Cada item que chega à etapa de relatório o mostra no terminal e salva um
Markdown separado na pasta de relatórios configurada.
Em `--cache-only`, textos ausentes geram também um arquivo automático por livro
nessa pasta. Com `--dry-run`, esses arquivos auxiliares continuam sendo salvos,
mas o EPUB traduzido não é publicado. Uma tradução parcial por falhas de trechos
ou ausências no cache, sem `--fail-fast` e com validação aceita, não conta como
falha do item.
Se não houver nome automático disponível para um auxiliar, o item falha;
`--continue-on-error` permite seguir para os próximos. Evite alterar caminhos
de entrada, saída ou relatórios durante a execução: ainda existe uma janela
entre a última verificação e a substituição ou criação exclusiva do arquivo.
Uma interrupção encerra o lote com os resultados dos itens iniciados e não inicia
o próximo livro. Se a gravação do relatório falhar, o item falha mesmo que seu
EPUB já tenha sido publicado. Uma recusa anterior à execução não mostra resumo
vazio.

### Traduzir capítulos selecionados

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --chapters "1-3,5,*chapter2*"
```

`--chapters` limita a tradução a documentos internos selecionados. Use índices
1-based, faixas e padrões separados por vírgula. Padrões com `*`, `?` ou `[`
usam glob simples; sem curinga, o Ayvu procura o trecho no título detectado, no
nome do item e no caminho interno do documento.

Antes de iniciar a tradução, o Ayvu mostra uma tabela `Selected chapters` com os
capítulos que serão processados. Capítulos fora da seleção permanecem no idioma
original e são copiados sem alteração para o EPUB gerado.

### Exportar CSV para revisão externa

```bash
uv run ayvu translate livro.epub \
  --source en \
  --target pt \
  --review-output livro-review.csv
```

O CSV v2 liga os textos à revisão do projeto, ao EPUB original, ao artefato da
tradução automática e às unidades estáveis. Ele contém texto sensível do livro;
mantenha o arquivo em armazenamento local protegido. Se o caminho já existir,
passe `--overwrite` ou escolha outro arquivo. `--dry-run` não gera pacote porque
não produz tradução revisável.

### Validar e aplicar uma revisão

Antes de aplicar, abra o CSV em um editor ou planilha. Nas linhas que quiser corrigir,
altere `reviewed_text`, que já vem com a tradução automática, e mude `review_status`
de `pending` para `reviewed`. As demais linhas podem ficar como estão e mantêm a
tradução automática. Se editar uma linha sem marcá-la, o pacote é recusado, para a
correção não se perder. Marcadores `__AYVU_REVIEW_PART_...__` indicam tags do EPUB:
mantenha cada um, na mesma ordem. As demais colunas ligam a linha ao livro e não
podem mudar. Salvar pela planilha é aceito, com vírgula ou ponto e vírgula.

```bash
uv run ayvu apply-review livro.epub livro-review.csv --validate-only
uv run ayvu apply-review livro.epub livro-review.csv
```

`--validate-only` verifica todas as linhas sem criar saída ou alterar o projeto.
A aplicação reconstrói e valida um EPUB novo; por padrão, salva
`<nome>-reviewed.epub`. Se esse caminho existir, a aplicação falha e preserva o
arquivo. Use `--overwrite` para substituir um EPUB revisado anterior. O original
e a tradução automática não podem ser destinos de aplicação. Repetir o mesmo
pacote com o mesmo destino confirma o resultado já gravado; se esse arquivo foi
apagado ou alterado, a aplicação roda de novo e segue as mesmas regras de destino.

Arquivos CSV v1 ainda podem ser inspecionados por ferramentas compatíveis, mas
`apply-review` informa `legacy_reexport_required` e orienta gerar um pacote v2.
O adaptador estrutural restaura markup usando placeholders emitidos para cada
unidade e pacote. Veja [`architecture/review-exchange.md`](architecture/review-exchange.md)
e [`architecture/review-application.md`](architecture/review-application.md).

### Sobrescrever saída existente

```bash
uv run ayvu translate livro.epub \
  --output livro-pt.epub \
  --source en \
  --target pt \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite \
  --overwrite
```

### Simular sem gravar EPUB

```bash
uv run ayvu translate livro.epub \
  --output teste.epub \
  --source en \
  --target pt \
  --dry-run
```

### Retomar a partir do checkpoint

O comando `resume` continua uma tradução em andamento usando o checkpoint salvo,
sem redigitar as opções:

```bash
uv run ayvu resume
uv run ayvu resume livro.epub --target pt
```

Sem argumentos, ele retoma a única tradução em andamento; havendo mais de uma,
informe o EPUB e o `--target`. O comando mostra o checkpoint (capítulos
concluídos, capítulo atual e falhas), os controles de execução salvos e
reexecuta a tradução com as opções da execução original. O cache evita
retraduzir o que já foi concluído, então só os trechos pendentes chamam o
tradutor.

A retomada é recusada se a mesma tradução ainda estiver rodando em outro processo,
se o livro tiver mudado ou se ele for recusado por política. Nesses casos nada é
alterado, nem o arquivo de estado.

### Reconstruir usando apenas o cache

O modo `--cache-only` monta o EPUB só com o que já está no cache e nunca chama o
tradutor (não precisa de servidor ativo). Trechos ausentes do cache ficam no
idioma original e são contados como `Texts missing` no relatório.

```bash
uv run ayvu translate livro.epub \
  --output teste.epub \
  --source en \
  --target pt \
  --cache .cache/traducoes.sqlite \
  --cache-only \
  --missing-output faltantes.txt
```

Por padrão, o EPUB é gerado mesmo com cobertura parcial. Para gerar somente
quando todos os trechos estiverem no cache, adicione `--require-full-cache`: se
faltar algum, nada é escrito e o comando falha, mas o arquivo de faltantes ainda
é gravado.

```bash
uv run ayvu translate livro.epub \
  --output teste.epub \
  --source en \
  --target pt \
  --cache .cache/traducoes.sqlite \
  --cache-only \
  --require-full-cache
```

Diferente da retomada (que chama o tradutor para os trechos pendentes e grava
estado em `~/Documentos/Livros/Processando`), o cache-only nunca chama o tradutor
e não cria estado de retomada.

### Extrair texto visível para Markdown

```bash
uv run ayvu extract livro.epub --output livro-extraido/
```

Um diretório não vazio exige `--overwrite`. Com essa opção, somente os arquivos
Markdown do plano são substituídos; arquivos sem relação são preservados. Cada
arquivo é publicado atomicamente. Se uma falha acontecer depois de publicações,
a mensagem informa quantos arquivos foram publicados e quantos não foram. O
comando não oferece transação para o diretório inteiro.

### Forçar modo comum ou desenvolvedor

A opção global `--mode` permite escolher o perfil de uso:

```bash
uv run ayvu --mode common translate livro.epub
```

```bash
uv run ayvu --mode developer translate livro.epub \
  --source en \
  --target pt \
  --url http://localhost:5000 \
  --cache .cache/traducoes.sqlite \
  --performance-profile custom \
  --requests-per-second 2 \
  --workers 2 \
  --retries 4 \
  --retry-backoff 1 \
  --retry-backoff-max 10
```

Use `--performance-profile` para escolher um preset de execução. `standard` é o
padrão conservador, `low` reduz pressão sobre máquinas e servidores fracos,
`high` usa `workers` paralelos para ambientes mais fortes, e `custom` começa do
padrão para ajuste manual por flags. Flags explícitas, como `--workers` ou
`--requests-per-second`, sobrescrevem o valor vindo do perfil.

Use `--requests-per-second` quando o servidor local ficar instável sob muitas chamadas. O retry
usa backoff exponencial para falhas de conexão, timeout, HTTP `429` e HTTP `5xx`.
Use `--workers` para processar documentos internos do EPUB em paralelo; o padrão
é `1`, e valores maiores criam tradutor e conexão SQLite separados por worker,
mantendo a montagem final em ordem. O plano e o checkpoint mostram `Workers`,
`Rate limit` e `Retry policy`; com workers paralelos, a barra de capítulos
indica que o progresso é reportado na ordem do EPUB. Se usar
`--translation-memory`, mantenha `--workers 1` nesta versão.

## Checklist recomendado

Antes da tradução completa:

1. Rode `test-translator`.
2. Rode `languages` se tiver dúvida sobre códigos de idioma.
3. Gere um preview.
4. Confira o preview no leitor de EPUB.
5. Prepare um glossário se o livro for técnico.

Depois da tradução:

1. Leia o relatório no terminal.
2. Salve o relatório Markdown quando quiser histórico local.
3. Abra o EPUB traduzido no leitor.
4. Confira capa, sumário, capítulos do começo e do meio, links internos, imagens e trechos com tags como itálico ou negrito.

## Limites atuais

- A biblioteca inicial lista e abre EPUBs, mas ainda não gerencia importação automática, fila ou histórico completo.
- Perfis ainda não têm editor guiado completo; edite `profiles` diretamente no arquivo de configuração.
- A tradução ainda acontece por nós de texto, então frases quebradas por tags podem perder contexto.
- A qualidade depende do servidor de tradução local.
- Livros técnicos costumam exigir glossário.
- EPUBs malformados podem depender do comportamento do parser HTML/XML.
