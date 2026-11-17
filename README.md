# Ayvu

Ayvu is a local-first command-line tool that translates EPUB books through a translation server running
on your own machine: any LibreTranslate-compatible HTTP endpoint, or a language model served by
[Ollama](docs/guide/ollama.md). It translates only the text a reader
sees, and keeps everything else as it was: HTML structure, CSS, images, links, table of contents and
internal file names. The original EPUB is never modified; the translation is written to a new file.

## Highlights

- **Structure-preserving translation:** whole paragraphs, headings and list items are translated, and
  inline tags such as `em`, `strong` and links survive through placeholders.
- **Resumable and cached:** every translated segment is cached in SQLite, so an interrupted book
  resumes where it stopped and a repeated text is never requested twice.
- **Durable projects:** each book becomes a local project with jobs, artifacts and provenance, so you
  can see what produced each output and resume a failed job safely.
- **Previews, dry runs and cache-only rebuilds:** translate the first chapters, simulate a run without
  writing an EPUB, or rebuild a book from the cache without calling the translator.
- **Batches and parallel workers:** translate up to 100 books in one command, with per-book reports,
  and process chapters in parallel while output, progress and checkpoints stay in book order.
- **Glossaries, profiles and translation memory:** keep technical terms consistent, save per-book
  presets, and reuse approved translations of similar sentences.
- **Human review:** export a CSV review package, correct it in a spreadsheet, and apply it back to
  produce a reviewed EPUB.
- **Offline by design:** an explicit network policy allows only loopback HTTP by default, or no
  network at all (`--network-policy none`).
- **Two modes:** a guided menu for everyday use (`ayvu`) and direct subcommands for scripting.

## Quick start

Requirements: Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) (or pip), and Docker to run the
translation server.

```bash
# 1. Install the CLI
git clone https://github.com/MichaelAlexandreDev/ayvu.git
cd ayvu
uv sync

# 2. Start a local LibreTranslate server
docker run -it -p 5000:5000 libretranslate/libretranslate

# 3. Check the connection
uv run ayvu test-translator --url http://localhost:5000

# 4a. Translate with the guided menu
uv run ayvu

# 4b. ... or directly
uv run ayvu translate book.epub --target pt
```

The guided menu asks for your default language and books folder on first use, then offers to translate
a book, generate a preview, open your library, manage glossaries and change settings.

Every new translation requires a model label declared by you, including batches, previews, dry runs
and cache-only rebuilds. Open **Settings (option 4) → 5. Change translator model label** in the guided
menu, or set `translator_models.libretranslate` in `config.json`. Guided translation and preview ask for
the label in place, save it and continue; Enter cancels. Without it, direct commands exit 1 before any
provider call, project, cache or output. Ayvu never invents a label or copies one from the server.
See [Model label, rich runtime and v2 cache](docs/guide/rich-runtime.md).

To translate with a model installed in Ollama instead, name it under `translator_models.ollama` and add
`--translator ollama`. See [Translating with Ollama](docs/guide/ollama.md).

## Common commands

| Command | What it does |
| --- | --- |
| `ayvu` | Opens the guided menu |
| `ayvu translate book.epub --target pt` | Translates one book, or up to 100 at once |
| `ayvu --preview book.epub` | Translates the first chapters into a preview EPUB |
| `ayvu translate book.epub --dry-run` | Simulates the run and reports, without writing an EPUB |
| `ayvu translate book.epub --cache-only` | Rebuilds the book from the cache, without calling the translator |
| `ayvu resume` | Resumes an interrupted translation |
| `ayvu inspect book.epub` | Shows title, authors, language and contents, read-only |
| `ayvu extract book.epub --output dir/` | Extracts the visible text to Markdown |
| `ayvu apply-review book.epub review.csv` | Applies a reviewed CSV package and writes a reviewed EPUB |
| `ayvu cache inspect\|clean\|export\|import` | Manages the translation cache |
| `ayvu languages` | Lists the languages your server offers; for Ollama (`--translator ollama`), the list Ayvu declares, since a model has no fixed one |

Run `uv run ayvu --help` or `uv run ayvu <command> --help` for every option.

## Documentation

- **[User guide](docs/README.md):** installation, translating, performance, glossaries, cache and
  resume, translation memory, the library, the rich runtime, configuration and the network policy.
- **[Architecture guide](docs/architecture/README.md):** how the code is organised, its module
  contracts and the decision records behind them.
- **[Contributing](CONTRIBUTING.md):** workflow, local development and pull requests.
- **[Rules for AI agents and contributors](AGENTS.md):** the architecture rules, validation steps and
  working agreement that every change follows.

## Engineering

- A modular monolith in layers, with dependency rules enforced by import-linter and architecture tests.
- More than 6,500 tests, run on every change plainly and in CI mode, plus a Docker stage on Ubuntu
  (system Python, 3.11 and 3.14), Debian, Fedora, Arch and a constrained target (1 CPU, 1 GB).
- A size ratchet keeps files under 400 lines and functions under 80; legacy outliers may only shrink.
- Behaviour changes ship with accepted decision records and side-by-side comparisons of real CLI runs.

To run the tests:

```bash
uv sync --extra dev
uv run pytest
```

## Usage notice

Ayvu is meant for personal use, study and accessibility with EPUB files you provide. It does not
remove DRM, does not download books, and must not be used to distribute copyrighted content. Books,
caches and private glossaries stay out of Git by default.

## Limitations

- Ayvu refuses protected content, unsafe or repeated internal file names, XML entity declarations, and
  books over its size and compression limits. Fonts with the standard EPUB obfuscation are accepted.
- If the computer sleeps for more than an hour during a translation, the run fails and nothing is
  published; what was translated stays in the cache, so repeating the command is fast.
- The same translation never runs twice at once; if the previous process was killed, repeating the
  command works right away.
- Malformed XHTML may depend on the parser's behaviour.
- Technical books usually need a glossary to keep terms consistent.
- The final quality depends on the translation server you use.

## License

Proprietary software; all rights reserved to S.Ralt. See [LICENSE](LICENSE). Versions up to commit
`975dbe7` were published under the MIT license.
Third-party components keep their own licenses; EbookLib is AGPL.
