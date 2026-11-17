# Translating books

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Modos de Uso

O Ayvu possui dois modos de execução que equilibram facilidade de uso com eficiência técnica:

- **Modo Comum (common)**: Focado em uma experiência guiada. Oferece sugestões de retomada de traduções interrompidas, convites para gerar previews e solicita confirmações antes de ações importantes (como sobrescrever arquivos). É o modo padrão ao executar apenas `ayvu`.
- **Modo Desenvolvedor (developer)**: Focado em execução direta e automação. Pula perguntas interativas e assume as configurações padrão ou passadas via argumentos. É o modo padrão ao utilizar subcomandos como `translate` ou `inspect`.

Para um passo a passo dos dois fluxos, leia o
[`tutorial de modo comum e modo desenvolvedor`](../tutorial-modo-comum-e-dev.md).

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

O comando é somente de leitura: mostra caminho, título, autores, idioma e as
contagens de documentos e itens, sem criar cache ou projeto. Livro protegido por
DRM recebe só um aviso, sem título ou autores. `translate` e `extract` o recusam. Em erros de leitura,
o modo desenvolvedor mostra um código de motivo seguro em vez do texto interno
do parser.

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

O comando aceita até 100 EPUBs e os processa sequencialmente, na ordem informada.
Sem `--source`, o idioma de origem é identificado separadamente nos metadados
de cada livro, que podem ter idiomas diferentes. Fora de `--dry-run`, cada livro
gera um EPUB separado com o padrão
`<nome>-<idioma>.epub` dentro de `--output-dir`. Se `--output-dir` não for
informado, o Ayvu usa a pasta de traduzidos configurada. O comando para no
primeiro erro por padrão; com `--continue-on-error`, continua nos livros
restantes e termina com código 1 se algum item falhar. Saídas existentes não
são sobrescritas sem confirmação no modo comum ou sem `--overwrite` no modo
desenvolvedor. Destinos de EPUB duplicados são recusados antes do início; no
modo comum, as escolhas para conflitos são feitas antes de processar os livros.

As opções `--output`, `--review-output` e `--missing-output` são exclusivas do fluxo de um único
EPUB, porque um caminho único de saída ou CSV não é suficiente para representar
vários livros. O batch salva automaticamente um relatório Markdown separado por
livro que chega à etapa de relatório, na pasta de relatórios configurada.
Em `--cache-only`, quando há textos ausentes, cada livro também recebe um
arquivo de textos ausentes com nome automático nessa pasta. No `--dry-run`,
o relatório e o arquivo de textos ausentes continuam sendo salvos, mas o EPUB
não é publicado. Uma tradução parcial por falhas de trechos ou ausências no cache,
sem `--fail-fast` e com validação aceita, não conta como falha do item.
With a model label, a provider failure always stops that book without a partial EPUB, with or
without `--fail-fast` and with any number of workers; in a batch, `--fail-fast` still stops the
batch after the first failed book, and the failed book resumes from its completed documents.
Se os nomes automáticos disponíveis se esgotarem, o item falha e a opção
`--continue-on-error` determina se os demais continuam. As verificações de
destino reduzem riscos, mas alterações concorrentes nos caminhos ainda podem
ocorrer entre a última verificação e a substituição ou criação exclusiva do arquivo.
Uma interrupção encerra o lote sem iniciar o próximo livro e preserva o resultado
dos itens iniciados. Se o relatório não puder ser salvo, o item falha; um EPUB
já publicado continua no destino. Uma recusa antes da execução não mostra um
resumo vazio.

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

`--review-output` gera um pacote CSV v2 vinculado ao projeto, à fonte exata, ao
artefato da tradução automática e às unidades estáveis. O pacote contém texto
sensível do livro e deve ficar em armazenamento local protegido. Se o CSV já
existir, use `--overwrite` ou escolha outro caminho. A opção não funciona com
`--dry-run`, porque o dry-run não gera texto traduzido revisável.

Para revisar, abra o CSV em um editor ou planilha. Nas linhas que quiser corrigir,
altere `reviewed_text`, que já vem com a tradução automática, e mude `review_status`
de `pending` para `reviewed`. As demais linhas podem ficar como estão e mantêm a
tradução automática. Se editar uma linha sem marcá-la, o pacote é recusado, para a
correção não se perder. Marcadores `__AYVU_REVIEW_PART_...__` indicam tags do EPUB:
mantenha cada um, na mesma ordem. As demais colunas ligam a linha ao livro e não
podem mudar. Salvar pela planilha é aceito, com vírgula ou ponto e vírgula.

Pacotes v1 antigos continuam disponíveis apenas para inspeção; `apply-review`
recusa sua aplicação e pede uma nova exportação v2. Para verificar um pacote v2
sem criar job, artefato ou EPUB, use `--validate-only`:

```bash
uv run ayvu apply-review livro.epub livro-review.csv --validate-only
```

Para aplicar, use o EPUB original e o pacote revisado. O padrão grava
`<nome>-reviewed.epub`; se esse destino existir, a aplicação falha sem alterá-lo.
`--overwrite` permite substituir um EPUB revisado anterior, nunca a fonte ou o
artefato traduzido automaticamente. Repetir o mesmo pacote com o mesmo destino
confirma o resultado já gravado; se esse arquivo foi apagado ou alterado, a
aplicação roda de novo e segue as mesmas regras de destino:

```bash
uv run ayvu apply-review livro.epub livro-review.csv
```

O mapeamento v2 aceita texto simples e placeholders emitidos para o próprio
pacote/unidade; o adaptador estrutural restaura os nós originais do EPUB. Veja
[`docs/architecture/modules/review.md`](../architecture/modules/review.md).

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
