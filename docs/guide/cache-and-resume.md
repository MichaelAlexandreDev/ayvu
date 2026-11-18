# Cache, resume and cache-only

## The translation cache

Translations are stored in SQLite. If the process is interrupted, run the same command again with the same
cache file:

```bash
uv run ayvu translate book.epub \
  --output book-pt.epub \
  --cache .cache/translations.sqlite
```

Segments already translated are reused automatically. The resume checkpoint keeps the translator's
execution options, such as timeout, workers, retries, requests per second and backoff.

New translations require a model label and use the independent v2 cache. Existing v1 jobs and old
standalone checkpoints are refused on resume and left unchanged; finish them with the frozen release
`v1-runtime-final` (see [Upgrading from the v1 runtime](upgrading-from-v1.md)). The migration keeps the v1 data without turning it into exact v2 hits. Cache administration accepts
explicit namespaces; without a selection, it keeps the legacy behaviour. See the
[standalone v2 cache](../architecture/modules/knowledge-cache.md).
Before new work, a translation names an old cache that has texts for its languages and says that it is
not reused; see [Upgrading from the v1 runtime](upgrading-from-v1.md).

## Administering the cache

The cache can be inspected, cleaned, exported and imported separately from the translation commands:

```bash
uv run ayvu cache inspect --cache .cache/translations.sqlite
uv run ayvu cache clean --cache .cache/translations.sqlite --source en --target pt --dry-run
uv run ayvu cache clean --cache .cache/translations.sqlite --source en --target pt --yes
uv run ayvu cache export ayvu-cache.json --cache .cache/translations.sqlite
uv run ayvu cache import ayvu-cache.json --cache .cache/translations.sqlite
```

- **`cache inspect`** groups entries by source language, target language, count, and the dates of the
  first and last entry.
- **`cache clean`** accepts the filters `--source`, `--target` and `--before`; to clean everything, use
  `--all`. Real removal requires `--yes`, and `--dry-run` shows how many entries would be deleted without
  changing the SQLite file. A selection that changed between the preview and the removal is rejected; run
  the command again in that case.
- **`cache export`** writes readable JSON, and **`cache import`** reads it. Existing entries are skipped by
  default and can be replaced with `--replace`.

If the cache does not exist yet, `inspect` shows zero entries and `export` writes an empty JSON, without
creating the cache or its folder. Detailed errors use short codes and never include entry contents. The
export destination must be a different file from the SQLite cache itself.

In the legacy v1 format, export and import are limited to 512 MiB and 1,000,000 entries; above that, export
in parts with `--source`, `--target` or `--before`. The v2 format has its own limits and filters, described
in [cache administration](../architecture/modules/knowledge-cache.md).

The exported file contains original and translated text. Treat that JSON as private book content, like the
EPUB and the SQLite cache.

`cache import` stages the entries in a temporary file next to the cache,
`.ayvu-cache-import-legacy-*.sqlite`, and only then changes the cache. Ctrl+C deletes that file. If the
process is killed or the computer shuts down midway, the cache stays as it was, but the temporary file is
left behind: it contains book text and can be deleted when no import is running.

v2 cache administration accepts an explicit namespace selection and imports with an external map and a
preview. See [cache administration](../architecture/modules/knowledge-cache.md) for examples, scope
authorisation and limits. Export and recovery files may contain sensitive text.

## Local projects

Direct, guided and batch translations create or reuse a local project automatically, in
`<processing folder>/projects/<UUID>/project.json`.

- **The manifest** stores the original's identity, reference and hash, the languages, and the
  configuration references. It holds private metadata, never the book's text or credentials.
- **Changing** the language or the configuration creates another project and keeps the previous one.
  Prompts, output names and the original EPUB are preserved.
- **`--cache-only`** also resolves a project; `--dry-run`, previews and resuming old checkpoints still
  need no manifest.
- **The default processing folder** is `~/Documentos/Livros/Processando`.

Persistence failures or ambiguities stop the translation before the translator's preflight. There is no
automatic cleanup: removing project state never deletes originals, outputs, the cache or glossaries, but it
loses the identity and references that history and per-project resume rely on. Symbolic links and sources
with hard links are refused in this flow. For the schema, limits and recovery from an interrupted write,
see the [local manifest contract](../architecture/modules/projects.md).

Each project also has a SQLite database, `projects/<UUID>/project.sqlite`, with its backups in
`backups/schema/`. Composition bootstraps and migrates it when needed, and a project translation creates
it on first use; the v1 manifest and the independent cache are preserved. See the
[migration and recovery protocol](../architecture/modules/projects-storage.md) for verified backups,
conflicts, process crashes and manual recovery.

That database holds the [durable job record](../architecture/modules/jobs.md), the
[artifact and checkpoint record](../architecture/modules/artifacts.md) and the
[provenance record](../architecture/modules/provenance.md). Each project translation becomes a job there,
from start to final result, and the [use case](../architecture/modules/translation-project.md) explains
what happens in each case. Dry runs and previews have no project and create no job.

In this flow, the translator URL must not contain a username, password, query parameters or fragments.
Authentication through credential references is future work; manifest fingerprints remain private
metadata.

## Checkpoints

During real translations, Ayvu also writes a local checkpoint of the run in `~/Documentos/Livros/Processando`,
or in the configured processing folder (a `*.ayvu-state.json` file). Besides the run's paths and options,
the checkpoint is updated **after each chapter** and records the progress: the total chapters, the
current chapter, the completed chapters, the failed chapters and how many segments failed. It does not
replace the cache and is not deleted automatically.

## Resuming a translation

To resume an interrupted translation without retyping its options, use `resume`:

```bash
uv run ayvu resume
uv run ayvu resume book.epub --target pt
```

Without arguments, `resume` continues the only translation in progress; when there are several, give the
EPUB and `--target` to choose. The command shows the checkpoint (how far it got) and the saved execution
controls (`Workers`, `Rate limit` and `Retry policy`), and runs the translation again with the saved
options. Running just `uv run ayvu` in common mode also looks for translations in progress and offers to
resume the one it finds, with the same checkpoint summary.

A resume never reopens a job that ended: it closes the interrupted job, if it has no result yet, and
creates a new one linked to it. It is refused if the same translation is still running in another
process, if the book changed since it started, or if the book is refused by policy. An old state file
without a matching job still works as before, without creating a project or a job. See
[translation recovery](../architecture/modules/recovery.md).

A resume runs only rich jobs: a v1 job is refused, with or without a model label, and its files stay as
they are. A rich job recovers its original provider and model from existing provenance, ignoring later
profile and configuration changes. See [Model label, rich runtime and v2 cache](rich-runtime.md).

The translator may be called again for a segment that was in progress when the run was interrupted.

The checkpoint and the cache complement each other: the **cache** keeps each translated segment so it is
never translated again, while the **checkpoint** records progress and failures. Because the output EPUB is
only written whole at the end, a resume reprocesses every chapter; thanks to the cache, only the segments
not cached yet (the ones that were missing or failed before) actually call the translator. That is why the
checkpoint and the cache must use the same paths across runs.

## Cache-only mode

`--cache-only` rebuilds the EPUB using **only** what is already in the cache, and **never** calls the
translator. It is useful to test the reconstruction, to reuse earlier work in a controlled way, and to
avoid accidental calls to the translator. Since there is no network traffic, no LibreTranslate server needs
to be running.

```bash
uv run ayvu translate book.epub \
  --output book-pt.epub \
  --cache .cache/translations.sqlite \
  --cache-only \
  --missing-output missing.txt
```

- **Cached segments** are applied, with the glossary when one is configured.
- **Segments missing from the cache** stay in the original language and are counted as `Texts missing` in
  the report.
- **Partial coverage:** by default, the EPUB is generated even with partial coverage. Use
  `--require-full-cache` to generate it only when every segment is cached; if anything is missing,
  nothing is written and the command fails.
- **`--missing-output PATH`** saves the original segments missing from the cache. Without it, a cache-only
  run with missing segments writes the file automatically in the Reports folder. The file is written even
  when `--require-full-cache` blocks the output, to show what still needs translating.

Compared with a normal resume: a resume runs the translation again and **calls the translator** for the
segments not cached yet, and writes state in `~/Documentos/Livros/Processando`. Cache-only never calls the
translator, creates no resume state and needs no running server.

Without a model label, every new cache-only run is
refused before any project, cache, job, output or provider call. Declare it through Settings option 5
or `translator_models.libretranslate` in `config.json`. With a label, cache-only runs with several workers.
