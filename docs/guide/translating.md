# Translating books

Every new LibreTranslate translation requires a declared model label, including previews, dry runs and cache-only
rebuilds. In the guided menu, open **Settings (option 4) → 5. Change translator model label**, or set
`translator_models.libretranslate` in `config.json`. Menus 1 and 2 also ask for a missing label in place;
Enter cancels. See [model labels](rich-runtime.md#declaring-the-model-label) for the format and example.

## Modes

Ayvu has two modes that balance ease of use with direct control:

- **Common mode** is guided. It offers to resume interrupted translations, invites you to generate
  previews, and asks for confirmation before important actions such as overwriting files. It is the
  default when you run just `ayvu`.
- **Developer mode** is direct and suited to automation. It skips interactive questions and uses the
  defaults or the arguments you pass. It is the default for subcommands such as `translate` or `inspect`.

For a step-by-step walkthrough of both, read the [tutorial](../tutorial-modo-comum-e-dev.md).

You can force a mode with the global `--mode` option:

```bash
uv run ayvu --mode common translate book.epub
```

List the languages your local LibreTranslate offers:

```bash
uv run ayvu languages --url http://localhost:5000
```

## Inspecting a book

```bash
uv run ayvu inspect book.epub
```

The command is read-only: it shows the path, title, authors, language and the document and item counts,
without creating a cache or a project. A DRM-protected book only gets a notice, without title or authors;
`translate` and `extract` refuse it. On read errors, developer mode shows a safe reason code instead of the
parser's internal text.

## Translating a book

```bash
uv run ayvu translate book.epub \
  --target pt \
  --translator libretranslate \
  --url http://localhost:5000 \
  --cache .cache/translations.sqlite
```

Translate with a profile saved in the configuration:

```bash
uv run ayvu translate book.epub --profile technical
```

Manage the translation cache (see [Cache, resume and cache-only](cache-and-resume.md)):

```bash
uv run ayvu cache inspect --cache .cache/translations.sqlite
uv run ayvu cache clean --cache .cache/translations.sqlite --source en --target pt --dry-run
uv run ayvu cache export ayvu-cache.json --cache .cache/translations.sqlite
uv run ayvu cache import ayvu-cache.json --cache .cache/translations.sqlite
```

Resume an interrupted translation from its saved checkpoint:

```bash
uv run ayvu resume
uv run ayvu resume book.epub --target pt
```

## Choosing the translator and model

`--translator libretranslate|ollama` selects an available provider. `--model` selects its declared label
or installed model, overriding the profile and `translator_models`. LibreTranslate uses
`http://localhost:5000`; Ollama uses `http://localhost:11434`. Use `--url` for another local server.

```bash
uv run ayvu translate book.epub --translator libretranslate --model local-model
uv run ayvu translate book.epub --translator ollama --model qwen2.5:7b
uv run ayvu translate book.epub --profile technical --model llama3.2:3b
uv run ayvu --preview book.epub --translator libretranslate --model local-model
```

The same selection applies to every book in a batch. Unknown providers and invalid models stop before
job or provider work. Ollama previews and cache-only runs remain unavailable; an Ollama dry run estimates tokens and cost. Guided translation
asks for the translator after the EPUB and profile only when there is a choice (an Ollama model in the
configuration or a profile naming a translator), then asks for a missing model. Settings option 6 lists
Ollama models or accepts a validated manual name. See [configuration and profiles](configuration.md).
A durable resume keeps the original provider/model even when configuration changes. The plan and both
report formats show `Provider` and `Model`, without credentials.

## Translating several books

```bash
uv run ayvu translate book-1.epub book-2.epub book-3.epub \
  --target pt \
  --output-dir translated/ \
  --continue-on-error
```

The command accepts up to 100 EPUBs and processes them one after another, in the order given.

- **Source language:** without `--source`, the source language is read from each book's metadata
  separately, so books may have different languages.
- **Output:** outside `--dry-run`, each book gets its own EPUB, named `<name>-<language>.epub` inside
  `--output-dir`. Without `--output-dir`, Ayvu uses the configured translated-books folder.
- **Errors:** the batch stops at the first error by default. With `--continue-on-error`, it goes on with
  the remaining books and exits with code 1 if any item failed.
- **Existing outputs** are not overwritten without confirmation in common mode, or without
  `--overwrite` in developer mode. Duplicate EPUB destinations are refused before the batch starts; in
  common mode, every conflict is decided before any book is processed.

`--output`, `--review-output` and `--missing-output` only work with a single EPUB, because one output
path or CSV cannot represent several books.

- **Reports:** the batch saves a separate Markdown report for each book that reaches the report stage,
  in the configured reports folder. With `--cache-only`, a book with missing texts also gets a
  missing-texts file with an automatic name in that folder. With `--dry-run`, the report and the
  missing-texts file are still saved, but no EPUB is published.
- **Partial translations:** a translation left partial by failed segments or cache misses, without
  `--fail-fast` and with an accepted validation, does not count as a failed item.
- **With a model label,** a provider failure always stops that book without a partial EPUB, with or
  without `--fail-fast` and with any number of workers. In a batch, `--fail-fast` still stops the batch
  after the first failed book, and the failed book resumes from its completed documents.
- **Names:** if the available automatic names run out, the item fails, and `--continue-on-error`
  decides whether the others go on.
- **Concurrency:** the destination checks reduce risk, but paths can still change concurrently
  between the last check and the replacement or exclusive creation of the file.
- **Interruption** ends the batch without starting the next book and keeps the results of the books
  already started.
- **Report failures:** if a report cannot be saved, the item fails; an EPUB already published stays at
  its destination. A refusal before the run shows no empty summary.

## Output location

Without `--output`, Ayvu saves by default to:

```text
~/Documentos/Livros/Traduzidos/book-pt.epub
```

In **common mode**, Ayvu shows the default output folder and the computed name of the translated EPUB, and
asks whether to keep that location before translating. To use another path, answer no and enter the path.
In **developer mode**, use `--output` to choose the output path yourself:

```bash
uv run ayvu translate book.epub \
  --output book-pt.epub
```

In **common mode**, if the output already exists, Ayvu shows the computed path and lets you overwrite it,
choose another name, or cancel without touching the existing file. To skip the question and overwrite
directly:

```bash
uv run ayvu translate book.epub \
  --output book-pt.epub \
  --overwrite
```

## Dry runs and cache-only rebuilds

Simulate a translation without writing an EPUB:

```bash
uv run ayvu translate book.epub \
  --output test.epub \
  --dry-run
```

With Ollama or a remote provider, a dry run also estimates tokens and cost without sending anything, and
`ayvu --max-cost <USD> translate ...` caps a run's cost; see [usage, cost and the cost cap](usage-and-cost.md).

Rebuild from the cache only, without calling the translator (see
[Cache, resume and cache-only](cache-and-resume.md#cache-only-mode)):

```bash
uv run ayvu translate book.epub \
  --output test.epub \
  --cache-only \
  --missing-output missing.txt
```

## The final report

At the end of a translation, Ayvu shows a report in the terminal with:

- the original EPUB, the languages and the computed output;
- the review file, when requested;
- the chapters processed, texts translated and texts from the cache;
- the texts reused from translation memory and the review suggestions, when memory is on;
- the texts missing from the cache, in cache-only mode;
- the errors;
- a summary of glossary usage, when a glossary is configured.

In **common mode**, it also asks whether to save the report as Markdown in `~/Documentos/Livros/Relatorios`.
Batch translations save a separate Markdown report for each book automatically.

## Extracting text

Extract the visible text to Markdown:

```bash
uv run ayvu extract book.epub \
  --output book-extracted/
```

Without `--overwrite`, a non-empty output directory is refused. With it, Ayvu replaces only the planned
Markdown files and keeps every other file. Each Markdown file is published atomically; if a failure happens
after some files were published, the message says how many were completed and how many were not.
Publication is independent per file, with no transaction for the whole directory.

## Selecting chapters

```bash
uv run ayvu translate book.epub \
  --target pt \
  --chapters "1-3,5,*chapter2*"
```

`--chapters` accepts 1-based indexes, ranges and patterns separated by commas. Patterns with `*`, `?` or
`[` use simple globbing; without a wildcard, Ayvu matches a substring. The selection is compared with the
detected title, the item name and the document's internal path in the EPUB. Before translating, Ayvu shows
the `Selected chapters` table. Unselected chapters are copied unchanged to the output EPUB.

## Source language

Without `--source`, Ayvu reads the book's language from its metadata, shows the translation plan
(`From`/`To`) before starting, and uses the detected language as the source. When the metadata is missing
or invalid, Ayvu warns and uses `en`; pass `--source` to choose another source language.

## Metadata and navigation

By default, Ayvu keeps the EPUB's metadata and navigation document. To also translate the book title in
the OPF and the EPUB 3 table of contents, use:

```bash
uv run ayvu translate book.epub \
  --target pt \
  --translate-metadata
```

The option is conservative: it translates the first `dc:title` and the navigation marked in the manifest
with `properties="nav"`, and keeps identifiers, authors, publisher and technical metadata. Changing the
title or the table of contents can affect how readers and libraries organise the book, so check the
generated EPUB before using the translation as your main copy.

## Image alternative text

By default, Ayvu keeps the alternative text (`alt`) of images. To also translate these accessibility
descriptions, use:

```bash
uv run ayvu translate book.epub \
  --target pt \
  --translate-alt-text
```

The option translates only the `alt` attribute of `<img>` tags, and keeps the image, its `src` and its
other attributes. Decorative images (`alt=""`) are skipped. The report then shows how many alternative
texts were translated. Reading text inside images (OCR) is out of scope.

## Human review

Generate an optional CSV for external human review:

```bash
uv run ayvu translate book.epub \
  --target pt \
  --review-output book-review.csv
```

`--review-output` produces a v2 CSV package bound to the project, the exact source, the machine
translation artifact and the stable units. The package contains the book's text, which is sensitive, so
keep it in protected local storage. If the CSV already exists, use `--overwrite` or choose another path.
The option does not work with `--dry-run`, because a dry run produces no translated text to review.

To review, open the CSV in an editor or spreadsheet:

- In the rows you want to correct, change `reviewed_text`, which starts with the machine translation,
  and change `review_status` from `pending` to `reviewed`.
- The other rows can stay as they are and keep the machine translation.
- If you edit a row without marking it, the package is refused, so that the correction is not lost.
- `__AYVU_REVIEW_PART_...__` markers stand for EPUB tags: keep each one, in the same order.
- The other columns tie the row to the book and must not change.
- Saving from a spreadsheet is accepted, with commas or semicolons.

Old v1 packages remain available for inspection only; `apply-review` refuses to apply them and asks for a
new v2 export. To check a v2 package without creating a job, artifact or EPUB, use `--validate-only`:

```bash
uv run ayvu apply-review book.epub book-review.csv --validate-only
```

To apply, use the original EPUB and the reviewed package:

```bash
uv run ayvu apply-review book.epub book-review.csv
```

- **Destination:** the default writes `<name>-reviewed.epub`. If that destination exists, the
  application fails without changing it.
- **`--overwrite`** allows replacing an earlier reviewed EPUB, never the source or the machine
  translation artifact.
- **Repeating** the same package with the same destination confirms the result already written. If that
  file was deleted or changed, the application runs again under the same destination rules.

The v2 mapping accepts plain text and the placeholders issued for that package and unit; the structural
adapter restores the EPUB's original nodes. See [`docs/architecture/modules/review.md`](../architecture/modules/review.md).

## Previews

Generate a translated preview:

```bash
uv run ayvu --preview book.epub
```

The preview translates the first internal documents of the EPUB, keeps the rest of the structure, and is
saved by default to:

```text
~/Documentos/Livros/Preview/book-preview.epub
```

## The guided menu

On first use, common mode asks for your default reading and translation language and the base books
folder, and shows the names of the feature folders. You can keep the default names or change them once
before the configuration is saved. From then on, that language is the default target for translations and
previews, and the base folder organises the library, previews, reports and translations, without asking
again.

Running just `uv run ayvu` opens the guided menu, with options to translate a book, generate a preview,
open the library, manage glossaries, open the settings, show help or exit.

- **Library:** lists the EPUBs in the `Original` and `Traduzidos` folders, shows the versions available
  for each book, and opens the original or a translation in the configured reader or the system's
  default one.
- **Settings:** shows and changes the default language, the base books folder, the feature folder names,
  the EPUB reader app, the translator model label and the configured translation profiles.
- **Target language:** in the guided translation and preview flows, the saved default target language is
  the first option. Choosing `Outro idioma` lists the languages LibreTranslate reports, with name, code
  and status, so you can pick an option or type a code. In developer mode, the target language is still
  set with `--target`.

If profiles are configured, the guided translation flow lets you choose one before the target language. A
profile can provide a different default language and an associated glossary. In developer mode, use
`--profile name`. Explicit flags such as `--target` and `--glossary` override the profile's values.

## Checks before translating

Before a translation starts, Ayvu checks the language pair, the glossary, the cache, the input EPUB and, for
real translations, the configured translator. If anything prevents the run, the command fails early with
a short message and a next step.

Ayvu also resolves the translation route from the languages LibreTranslate reports. If there is no direct
path between source and target, it tries an intermediate route through English (for example
`fr -> en -> pt`). When an intermediate route is used, common mode warns that the translation takes two
steps and that quality may suffer, and developer mode shows the route explicitly. If no route is available,
the command fails before processing the EPUB.
