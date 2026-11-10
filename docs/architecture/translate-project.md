# The translate-project use case

Related issue: [#145 — Extract the translate-project application use case](https://github.com/MichaelAlexandreDev/ayvu/issues/145)

`TranslateProject` translates one book of one stored project as one durable job, for any consumer. It imports no
framework, store, format or provider: it receives them as ports from a composition root.

`translate` and the guided translation run through it, one book at a time, and so does each book of a batch, and a
[resume](job-recovery.md) of a job it left behind. Dry run, preview and a resume file that recomputes to no job have no
project to run in, so they keep the current flow behind `translate_without_project`.

## Seleção explícita do runtime (#252)

`ayvu.cli_composition` resolve `translator_models` para o tradutor escolhido e
`ayvu.translate_project_composition` constrói `LegacyTranslationRuntime` ou
`RichTranslationRuntime`. Sem rótulo, a composição e o fingerprint legados permanecem
os mesmos. O rótulo válido (`[a-z0-9][a-z0-9_-]{0,47}`) produz o modelo
`declared:<rótulo>`; `unknown` não foi tornado elegível. **Trocar o modelo do servidor
sem atualizar o rótulo reutiliza resultados antigos sob a mesma declaração.**

O runtime rico prepara `SQLiteExactCache` em `<nome de --cache>.v2.sqlite` antes de
preparar o provedor; não abre nem migra o v1. Exige `--workers 1` e recusa
`--translation-memory`, `--cache-only` e `--require-full-cache`. Não há identidade/rota
offline fixada para oferecer cache-only rico nesta integração. Preflight sanitiza
recusas do cache. A autorização HTTP existente é reutilizada, sem novas requisições.

Um consumidor de aplicação pode injetar `ProjectTranslation.translation_context`
com snapshots e autorização explícitos. A [política de contexto limitado](translation-context.md)
prepara e valida a divulgação antes dos repositórios/provider, revalida na saída
de cada chamada e inclui o efeito no fingerprint do job somente quando declarado.
Isso não cria autorização implícita a partir de um perfil ou configuração da CLI.

O port recebe um `RuntimeContext` com job real, cancelamento e canal de eventos.
Antes de cada documento, a composição consulta o job atual para observar leases
renovados, mantendo o token do dono original para não adotar a posse de outro processo.
`TranslationResources.prepare_document` prepara as execuções de um documento antes de
o engine consumir suas unidades. Só a composição conhece `DocumentTranslator` e seus
repositórios. Exceções na preparação interrompem o livro; o engine e o caso de uso
não selecionam runtime. O engine mantém o progresso de capítulos; eventos internos de
progresso de documento não são intercalados com essa sequência. Avisos são encaminhados,
e cancelamento é emitido uma vez no encerramento.

O comando carrega identidade genérica de execução: revisão `1` no legado e `2` no
rico, produtor opcional e papéis adicionais de saída. A revisão é gravada em
`JobRequest.command_version`, com `translated_output` e `raw_results` em `output_roles`
no rico; a proveniência `execution_identity` e os registros de resultado guardam o
produtor declarado. O fingerprint do job inclui essas dimensões apenas quando declaradas.
A continuação compara as revisões antes de criar o novo job e recusa `runtime_mismatch`;
a CLI nomeia `legacy/v1` e `rich/v2`. O checkpoint rico guarda somente a revisão de
execução `2`; o rótulo não é copiado. A recuperação refaz o fingerprint com o produtor
já registrado no job, independentemente da configuração atual. Sem prova, recusa a
associação em vez de rebaixar a entrada para v1. Checkpoints legados omitem o campo novo.

Uma falha do provedor no rico deixa um job `failed`, retomável, sem EPUB parcial.
Segmentos já concluídos permanecem em v2; o retry usa esses valores pré-glossário.
O runtime aplica o glossário em cada execução, inclusive sobre acertos do cache.
Resultados brutos são produzidos por documento, mas seu replay de produção permanece
desativado até #242. Publicação, validação, relatório e exportação de revisão seguem
os mesmos ports do caminho legado, preservando o EPUB original.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.translate_project` | Application | The command and result, the ports, the plan and the job from creation to its outcome |
| `ayvu.translation_engine` | Application | One book through a format adapter and a unit executor |
| `ayvu.artifact_destination` | Infrastructure | Publishing to the file the user chose, and the way back to it after a crash |
| `ayvu.translate_project_composition` | Composition | The current language work and validator behind the ports, and the assembled use case |

## What happens, in order

1. **Refusals, with no job created.** The project must load, its language pair must match, and the source on disk must
   still be the revision the project records. A job store that cannot be opened stops here too, before any provider.
2. **Reconciliation.** Whatever an earlier run left behind in this project is settled first.
3. **Duplicates.** The run takes the plan's guard. If another live process holds it, the run is refused as
   `job_in_progress`. A plan that ended earlier is new work.
4. **The job.** `queued -> preflight`, then the provider and cache checks. Their failure ends the job as `failed`.
5. **The book.** `running`. The engine extracts, translates and reconstructs.
6. **Staging.** The bytes go to a temporary file next to the destination, are read back and checked.
7. **Validation** of that file, while the destination is still untouched.
8. **Publication**, atomically. Then `completed -> validating ->` the terminal outcome.

## Outcomes

| What happened | Job state | Reason code | Output |
| --- | --- | --- | --- |
| Everything worked | `validated` | `validated` | Published |
| Validation warnings | `partially_completed` | `validation_warnings` | Published, as today |
| A legacy text failed and stayed in the source language | `partially_completed` | `texts_failed` | Published, as today |
| Legacy cache-only, with gaps | `partially_completed` | `texts_missing` | Published, as today |
| Legacy `--require-full-cache`, with gaps | `partially_completed` | `cache_incomplete` | None, as today |
| The lineage could not be written | `partially_completed` | `provenance_failed` | Published |
| Publishing failed after the file became visible | `partially_completed` | `publication_uncommitted` | Published, and the failure is reported |
| Provider, format, conflict or lease failure | `failed` | The failure's code | None |
| Cancelled | `cancelled` | `cancelled` | None. An old file under overwrite stays |

A failure stores its code, never its message, which may quote the book or the provider's address. The exception stays
on the result in `raised`, for the presentation that chose this book.

`validating` comes after validation has already run, because a cancellation has to be able to land after validation
and before publication, and the job has no way from `validating` to `cancelled`. The state records that the output was
checked and published. This validator is the current `validate_output_epub`, behind a port, not the adapter's own.

## Publishing to the chosen file

The output goes straight to the destination the user chose. A copy in the project would use the disk again on every
run, with no retention defined yet, and would leave the book in plain text in a second place.

The artifact protocol does not change. The record is reserved, the bytes are staged and measured, and only then do they
become visible. `LocalDestinationPayloads` differs in where:

- **The temporary file** sits next to the destination, as `.ayvu-<artifact UUID>.part`, so publishing is one atomic
  step on one filesystem and no document name appears in it.
- **A new file** uses a hard link, which fails rather than replaces, so an output that appeared meanwhile is never
  overwritten and the job ends with `output_exists`.
- **`--overwrite`** swaps the file with `os.replace` only at the end. Until then the old file is intact. It keeps the
  old file's mode, and a link at the destination stays a link.
- **Without hard links**, as on exFAT, an exclusive create takes the name and the file is swapped over it. That still
  cannot overwrite anything, but an empty file is visible for a moment. A crash in that moment leaves it there, because
  Ayvu never removes a file at the destination. This path was tested only with an injected failure of the link call.

The artifact record derives its location from its ID and holds no path, so a journal in
`projects/<UUID>/artifacts/publications/<artifact UUID>.json` records the destination, durably, before anything exists
out there. It lives only as long as the publication does. The project folder already holds the source's path in its
manifest, so this adds no new kind of data there.

## Crashes and interruptions

Reconciliation runs at the start of every execution, and through `reconcile()`.

| Left behind | Artifact | Job | Files |
| --- | --- | --- | --- |
| Crash while writing | `abandoned` | `failed`, `interrupted` | Temporary file removed |
| Crash after staging | `abandoned` | `failed`, `interrupted` | Temporary file removed, old file intact |
| Crash right after publishing | `ready` | `partially_completed`, `interrupted_after_publication` | Output kept, temporary name removed |
| Crash after the record went ready | `ready` | `partially_completed`, `interrupted_after_publication` | Output kept |

A job is settled only if its lease has expired, if it has none in a state that needs one, or if it is on this run's own
plan and the guard shows its owner is gone. Any other job with a live lease is left alone. Ctrl+C is handled in place by
the same rules, and it still reaches the caller.

A crash is judged by the bytes. If a file with exactly the expected bytes appeared at the destination by other means,
the run cannot tell it from its own, and it ends as `publication_uncommitted` and not as a conflict.

## The run guard

A lease cannot say that its process died: after a `kill`, a closed terminal or a power cut it stays live for up to an
hour, and repeating the command would be refused for that whole time. So each run also holds one lock file per plan, in
`projects/<UUID>/runs/`, and the system releases it when the process ends, however it ends.

Holding the guard therefore proves that a live lease on this plan belongs to nobody, and the run takes that job over and
settles it. A job on another plan is left to its lease, and a queued job holds nothing and waits for its lease to expire.
The guard also makes the duplicate check atomic across processes. The lock files stay, because removing one could split a
lock between two processes.

Two runs of one book on different plans, such as different outputs, can still run together.

A run waits 50 ms for its guard, so that a reader [probing it](job-recovery.md#when-a-run-is-alive) cannot make it refuse.
A job left `queued` with no lease on this plan is settled here, because its creator died between creating it and taking
the lease, under this same guard.

## Leases

A separate thread renews the lease every 5 minutes, up to the 1-hour maximum, because one chapter or one blocked call
can outlast any safe point. A run that loses its lease cannot publish, because the artifact store checks it.

**A computer asleep for more than an hour loses the lease.** That run ends as `failed`, `lease_expired`. The cache keeps
what was translated, so running it again is quick. This is new: before #145 the same run would carry on after waking up.
The run keeps translating after it wakes, and it learns of the loss only when it tries to publish.

## Cancellation

`TranslateProject.cancellation()` returns the source to pass to `execute`. Requests are seen at `translate_before_start`,
before each chapter and unit, at the adapter's own safe points, and at the artifact's. A blocking provider call is not
interrupted: it is seen at the next safe point. A request after the last safe point arrives too late, and the job ends
as it would have. A worker that commits to cancelling wins over a failure in another worker.

## What it does not do

- **Dry run and preview are refused as commands.** They need no project and create none, and identity must never be
  made up to fit them, so they stay on the current flow behind `translate_without_project`.
- **It receives producer identity from composition.** Legacy lineage keeps `unknown` because v1 cannot identify its
  producer. Rich runs record the LibreTranslate adapter and the user-declared model; they do not probe the server.
- **It does not decide what to resume.** A job that ended stays ended, and `execute` only links a new job to it when
  given a ticket. [Job recovery](job-recovery.md) decides. The resume file is still kept by the CLI, chapter by chapter,
  around the use case, because it is the only record of the address, the cache and the output.
- **It does not write the review package itself.** `translate --review-output`
  passes the exact result to `ExportReviewPackage`; the shared review use case
  validates identity and publishes the v2 package through D106.

## What the CLI adds

`ayvu.cli` turns each failure code into a message in its own table, and shows the exception's text only in developer
mode. A book the adapter refuses by policy (protected content, unsafe names, duplicates, entities, the size and
compression limits) is refused by `check_book` on every path, including dry run, preview and `resume`, before any
translator is asked and before a resume file is written. A book the adapter merely cannot read is still judged by the
current preflight, with its current message.

A provider's empty `translatedText` stays a success on the legacy path. Rich runs refuse it as `empty_result` and
publish no partial EPUB.
