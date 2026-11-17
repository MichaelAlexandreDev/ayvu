# Model label, rich runtime and v2 cache

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Pipeline rico e cache v2 (opcional)

In guided mode, open Settings (option 4) and choose `5. Change translator model label`: it validates
the label with the rules below before saving, and `-` removes it.

Para ativar em `translate`, tradução guiada e cada livro de um lote, adicione manualmente
este campo ao **arquivo de configuração existente**, preservando os demais campos:

```json
{
  "translator_models": {
    "libretranslate": "meu-modelo-2026-11"
  }
}
```

O arquivo fica em `$XDG_CONFIG_HOME/ayvu/config.json`, ou `~/.config/ayvu/config.json`
quando a variável não está definida. O rótulo aceita de 1 a 48 caracteres ASCII:
letras minúsculas, números, `_` e `-`, começando por letra ou número. URLs, caminhos,
espaços e texto livre são recusados antes de chamar o tradutor. Não há rótulo padrão.

```bash
uv run ayvu translate livro.epub --workers 1 --cache ./cache.sqlite
```

Com o rótulo, o runtime `rich/v2` usa `DocumentTranslator` e grava em
`cache.sqlite.v2.sqlite`, no mesmo diretório do `--cache`. O arquivo v1 indicado por
`--cache` permanece intacto: nenhuma entrada é lida, copiada ou promovida para v2.
Por isso o primeiro uso rico traduz novamente textos que só estavam no v1. O cache v2
é separado por projeto e identidade semântica; o glossário é aplicado depois do cache.

O modelo é registrado na proveniência como `declared:meu-modelo-2026-11`: uma declaração
sua, sem consulta nem descoberta de modelo no servidor. **Se você trocar o modelo do
servidor e mantiver o rótulo, o Ayvu reutilizará resultados antigos sob esse rótulo.**
Troque o rótulo junto com o modelo. Restaurar um rótulo permite reutilizar seu cache anterior.
O rótulo aparece nos dados operacionais de proveniência e identidade do cache; não em logs,
eventos, relatório de terminal ou checkpoint. Não coloque dados pessoais no rótulo.
O checkpoint rico registra apenas a revisão de execução, para vincular a retomada ao job.

With a valid model label, `--cache-only` creates the normal project/job and
reconstructs from authorized exact entries in `<--cache>.v2.sqlite`, without DNS,
provider/session/discovery or cache writes. Each unit recovers its own compatible canonical
identity, including stored bridge routes. A missing database is empty; incompatible or ambiguous
identities preserve the source and generate the existing missing-text report. Corruption,
overflow and unsafe storage fail without an EPUB or a misleading missing-text file.
`--require-full-cache` blocks reconstruction before staging when any text is missing, preserves
previous output and exits 1; complete coverage reconstructs and validates with exit 0.
Cache-only excludes translation memory, fuzzy reuse and raw-results replay, and creates no new
raw-results even on complete hits. Durable cache-only accepts `--workers N` without clamping, including the `high` default of four and explicit overrides. Whole documents run concurrently; units, reported results, events and progress remain in book order; no checkpoint file is written. The admission window includes completed documents awaiting consolidation. Labeled online translation accepts the same workers: each worker owns its translator session, all of them share one rate limiter, and a text repeated across documents is translated once. Parallel governed memory remains refused, including with `high`. Cancellation or a document failure stops admission and joins started workers before the job ends.
Without a model label, cache-only retains the v1 behavior.

Com o rótulo configurado, `--translation-memory` seleciona o repositório governado
`approved-memory.sqlite` do projeto (#162). O padrão continua desligado. A ordem é:
cache v2 exato → memória aprovada exata → memória aprovada aproximada → compatibilidade
legada explicitamente habilitada → provedor. Só revisões atuais `approved` entram;
automação nunca aprova nem torna valores elegíveis para datasets.

A aplicação automática exata exige as mesmas revisões de pipeline, perfil e glossário,
placeholders compatíveis e estrutura de origem verificável no documento carregado.
Resultados de outros documentos são sugestões. Tags inline ficam no fluxo normal.
Correspondências fuzzy são sugestões por padrão, inclusive acima de `0.95`.
Para selecionar a política conservadora de aplicação fuzzy, execute:

```bash
uv run ayvu translate livro.epub --translation-memory \
  --tm-conservative-auto-apply --tm-apply-threshold 0.98
```

A compatibilidade com candidatos do cache exige, separadamente,
`--translation-memory --tm-legacy-cache-compatibility`. Esses candidatos permanecem
`machine_candidate`, sem revisão. Só valores v2 com identidade e estágio conhecidos
são aceitos; linhas v1 não têm esses metadados e são excluídas. A mera presença do
arquivo antigo ou o uso anterior de `--translation-memory` não habilita essa política.
Os dois novos seletores requerem o rótulo de modelo e `--translation-memory`.
With a model label, `--translation-memory` prints a line saying that the v1 cache is not used
as memory, and an explicit `--tm-apply-threshold` without `--tm-conservative-auto-apply` prints a
warning that fuzzy matches stay suggestions. The run continues; nothing changes without a label.

O CLI informa `memory_review_suggestions` e conta sugestões e textos de memória no
relatório existente. A consulta de concordância é uma API de aplicação para chamadores
autorizados; não há editor ou consumidor Desktop novo. Para criar/aprovar revisões,
use os casos de uso explícitos de #161, documentados em
[Memória aprovada](../architecture/modules/knowledge-memory.md); o CLI de tradução
não cria essas revisões. Limpar o cache não apaga a memória aprovada.

Uma falha do provedor para o runtime rico na primeira ocorrência e não publica EPUB parcial.
Os segmentos concluídos ficam em v2; corrija o problema e execute `uv run ayvu resume livro.epub`.
A resume keeps the runtime its job started with: a v1 job finishes on v1 even after a label is
declared, and a rich job needs a label (with another label it translates again and says why). A
retomada reaproveita os documentos que o job anterior concluiu, sem
chamar o tradutor nem o cache, quando prova que nada mudou: modelo, rota, capacidades,
configuração, livro e versão do pipeline. Se algo mudou, ela traduz de novo e avisa o que foi,
por exemplo `Aviso da tradução: raw_results_changed_provider`.

Without a label, `legacy/v1` keeps its messages, continuation after errors and partial cache-only EPUBs.
Removing the label restores the intact v1 cache for new translations. Unlabeled previews and dry runs
retain v1. With a label, root and guided previews use the transient rich runtime: authorized
discovery and translation, the first 12 documents, the configured Preview folder and overwrite
confirmation, without project/job creation, Markdown or raw-results. Persistent exact v2 hits
require an existing matching project. Raw provider results are reused only in memory during
the invocation, before applying the current glossary; no cache is warmed. Provider failure
returns 1 without publishing a partial EPUB or changing previous output. With a model label, `--dry-run` uses
an offline transient rich runtime: no network, project/job creation or cache writes; exact v2 hits
require an existing matching project. `--dry-run --cache-only --require-full-cache` exits 1 only
when texts are missing. Configured workers/high are retained, with sequential local analysis stated
in progress and reports. Unlabeled preview selection is unchanged. Durable labeled cache-only uses the offline route described above.
As políticas HTTP continuam as mesmas, sem novas requisições de descoberta.
