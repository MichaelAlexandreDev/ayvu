# Model label, rich runtime and v2 cache

## Declaring the model label

The rich translation runtime (`rich/v2`) runs when you declare the model label of your translator. The
simplest way is the guided menu: open **Settings (option 4)** and choose
**`5. Change translator model label`**. The label is checked with the rules below before it is saved, and
`-` removes it.

You can also add the field by hand to the **existing configuration file**, keeping its other fields:

```json
{
  "translator_models": {
    "libretranslate": "my-model-2026-11"
  }
}
```

The file is `$XDG_CONFIG_HOME/ayvu/config.json`, or `~/.config/ayvu/config.json` when the variable is not
set. A label has 1 to 48 ASCII characters: lowercase letters, digits, `_` and `-`, starting with a letter
or digit. URLs, paths, spaces and free text are refused before the translator is called. There is no
default label. The label applies to `translate`, guided translation and every book of a batch.

```bash
uv run ayvu translate book.epub --workers 1 --cache ./cache.sqlite
```

## What changes with a label

- **The v2 cache:** the rich runtime uses `DocumentTranslator` and writes to `cache.sqlite.v2.sqlite`, in
  the same directory as `--cache`. The v1 file given in `--cache` stays intact: no entry is read, copied
  or promoted to v2, so the first rich run translates again texts that were only in v1. The v2 cache is
  separated by project and semantic identity, and the glossary is applied after the cache.
- **Provenance:** the model is recorded as `declared:my-model-2026-11`. It is your declaration; Ayvu does
  not query or discover the server's model.
- **Changing models:** **if you change the server's model and keep the label, Ayvu reuses old results
  under that label.** Change the label together with the model. Restoring a label lets you reuse its
  earlier cache.
- **Where the label appears:** in the operational provenance data and in the cache identity; not in logs,
  events, the terminal report or the checkpoint. Do not put personal data in the label. The rich
  checkpoint records only the execution revision, to tie a resume to its job.

## Cache-only and workers

With a valid model label, `--cache-only` creates the normal project and job, and rebuilds the book from
authorised exact entries in `<--cache>.v2.sqlite`, with no DNS, provider, session, discovery or cache
writes.

- **Identities:** each unit recovers its own compatible canonical identity, including stored bridge
  routes.
- **Missing or unusable entries:** a missing database is empty; incompatible or ambiguous identities keep
  the source text and produce the usual missing-text report.
- **Hard failures:** corruption, overflow and unsafe storage fail without an EPUB or a misleading
  missing-text file.
- **`--require-full-cache`** blocks the rebuild before staging when any text is missing, keeps the
  previous output and exits 1; complete coverage rebuilds and validates with exit 0.
- **What it never does:** cache-only excludes translation memory, fuzzy reuse and raw-results replay, and
  creates no new raw results even on complete hits.

**Workers:**

- **Cache-only** accepts `--workers N` without clamping, including the `high` profile's four workers and
  explicit overrides.
- **Order:** whole documents run concurrently, while units, reported results, events and progress stay in
  book order. The admission window counts completed documents still waiting to be consolidated.
- **Online translation** accepts the same workers. Each worker owns its translator session, all of them
  share one rate limiter, and a text repeated across documents is translated once.
- **Governed memory** with several workers is still refused, including with `high`.
- **Stopping:** a cancellation or a document failure stops admission and waits for the started workers
  before the job ends.

## Translation memory

With a label, `--translation-memory` uses the project's governed repository, `approved-memory.sqlite`. It
is still off by default. The lookup order is:

1. the exact v2 cache;
2. exact approved memory;
3. approximate approved memory;
4. the legacy compatibility source, when explicitly enabled;
5. the provider.

Only current `approved` revisions are used; automation never approves anything or makes values eligible
for datasets.

Exact automatic reuse requires the same pipeline, profile and glossary revisions, compatible placeholders,
and a source structure that can be verified in the loaded document. Results from other documents are
suggestions, and inline tags follow the normal flow. Fuzzy matches are suggestions by default, even above
`0.95`. To select the conservative fuzzy auto-apply policy, run:

```bash
uv run ayvu translate book.epub --translation-memory \
  --tm-conservative-auto-apply --tm-apply-threshold 0.98
```

Compatibility with cache candidates requires, separately, `--translation-memory
--tm-legacy-cache-compatibility`. These candidates stay `machine_candidate`, unreviewed. Only v2 values with
a known identity and stage are accepted; v1 rows lack that metadata and are excluded. Neither the presence
of the old file nor an earlier use of `--translation-memory` enables this policy. Both selectors require
the model label and `--translation-memory`.

With a model label, `--translation-memory` prints a line saying that the v1 cache is not used as memory, and
an explicit `--tm-apply-threshold` without `--tm-conservative-auto-apply` prints a warning that fuzzy
matches stay suggestions. The run continues; nothing changes without a label.

The CLI reports `memory_review_suggestions`, and counts suggestions and memory texts in the existing
report. The concordance query is an application API for authorised callers. To create or approve
revisions, use the explicit use cases documented in
[Approved memory](../architecture/modules/knowledge-memory.md); the translation CLI does not create them.
Cleaning the cache does not delete approved memory.

## Failures and resume

A provider failure stops the rich runtime at its first occurrence and publishes no partial EPUB. The
completed segments stay in v2; fix the problem and run `uv run ayvu resume book.epub`.

A resume keeps the runtime its job started with: a v1 job finishes on v1 even after a label is declared,
and a rich job needs a label (with another label, it translates again and says why). The resume reuses the
documents the previous job completed, without calling the translator or the cache, when it can prove that
nothing changed: model, route, capabilities, configuration, book and pipeline version. If something
changed, it translates again and says what, for example `Aviso da tradução: raw_results_changed_provider`.

## Without a label, previews and dry runs

- **Without a label,** `legacy/v1` keeps its messages, continuation after errors and partial cache-only
  EPUBs. Removing the label restores the intact v1 cache for new translations. Unlabeled previews and dry
  runs stay on v1.
- **Previews with a label** (root and guided) use the transient rich runtime:
  - authorised discovery and translation of the first 12 documents;
  - the configured Preview folder and the overwrite confirmation;
  - no project, job, Markdown or raw results.

  Persistent exact v2 hits require an existing matching project. Raw provider results are reused only in
  memory during the invocation, before the current glossary is applied, and no cache is warmed. A
  provider failure returns 1 without publishing a partial EPUB or changing the previous output.
- **`--dry-run` with a label** uses an offline transient rich runtime: no network, no project or job, and no
  cache writes. Exact v2 hits require an existing matching project. `--dry-run --cache-only
  --require-full-cache` exits 1 only when texts are missing. Configured workers and `high` are kept, and the
  progress and reports state that local analysis is sequential.

The HTTP policies stay the same, with no new discovery requests.
