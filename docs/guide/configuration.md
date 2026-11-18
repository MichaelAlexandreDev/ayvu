# Configuration

Ayvu keeps local preferences in a configuration file. In common mode, the first run asks for the default
language and saves the configuration. Afterwards, `Settings` lets you change the default language, the base
books folder, the feature folder names, the EPUB reader app and the translator model label. The file is:

```text
$XDG_CONFIG_HOME/ayvu/config.json
```

When `XDG_CONFIG_HOME` is not set, the fallback is:

```text
~/.config/ayvu/config.json
```

The initial format:

```json
{
  "version": 1,
  "default_target_language": "pt",
  "books_dir": "~/Documentos/Livros",
  "folders": {
    "original": "Original",
    "translated": "Traduzidos",
    "preview": "Preview",
    "reports": "Relatorios",
    "processing": "Processando"
  },
  "reader_app": null,
  "profiles": {}
}
```

Every new translation with LibreTranslate requires a model label in `translator_models.libretranslate`. Set
it through Settings (option 4), option 5, or edit the existing configuration. Guided translation and preview
also ask in place, save and continue; Enter cancels. See
[Model label, rich runtime and v2 cache](rich-runtime.md). The `ollama` entry names an installed Ollama
model instead; see [Translating with Ollama](ollama.md).

## Translation profiles

Profiles can be added by hand under `profiles`:

```json
{
  "profiles": {
    "technical": {
      "target_language": "pt",
      "glossary": "technical.json",
      "style": "technical",
      "translator": "ollama",
      "model": "qwen2.5:7b"
    }
  }
}
```

- **`translator`** selects `libretranslate` or `ollama`; omitting it keeps LibreTranslate.
- **`model`** declares a LibreTranslate label or names an installed Ollama model. Validation uses the
  profile's translator (LibreTranslate if omitted). Invalid selections stop before provider or job work.
- **`target_language`** sets the default target language when `--target` is not given.
- **`glossary`** accepts an absolute path, or a path relative to the local glossaries folder.
- **`style`** accepts `neutral`, `technical`, `literary` or `academic`. With the current LibreTranslate
  backend it is only profile metadata: it changes neither the prompt nor the translator's behaviour.

The contract of [versioned project profiles](../architecture/modules/projects-profiles.md) allows an
explicit migration by application callers. The CLI keeps using the global v1 profiles in this
compatibility window; there is no new migration command and no automatic migration. The migration requires
a glossary from the managed catalog and keeps the global profile.

For the fields that translation profiles cover, the precedence is:

```text
CLI arguments > selected profile > configuration file > built-in defaults
```

`ayvu translate book.epub --profile technical --model llama3.2:3b` overrides the profile's model.
`--translator libretranslate --model local-model` overrides both. A profile model belongs to its
translator: switching providers without `--model` uses the selected provider's `translator_models`
entry. Flags never rewrite configuration. No global default translator or new connection format is added.
A durable resume uses its job's original provider and model, ignoring later profile/configuration changes.

## Guided translator settings

Open Settings (option 4), then **6. Choose translator and model**. Choose `libretranslate` to declare
its label, or `ollama` to list installed model names from the local server. Enter a list number or a
validated name; if discovery fails, manual entry is still available. This records only
`translator_models.<translator>`; translation still requires an explicit Ollama choice, or an Ollama
profile. Enter cancels without saving. Option 5 continues to edit only LibreTranslate's label.
Guided translation offers the profile's translator as its default and uses the selected provider for
language discovery. Preview accepts the same provider selection, but Ollama preview remains unavailable.

## Folders and the reader

Without explicit paths, the base folder and the configured folder names decide where Ayvu saves previews,
translations, Markdown reports and processing state, and which folders the library uses. `reader_app` sets
the command used to open EPUBs from the library when the system's default reader is not enough.

Glossaries created in common mode live next to the configuration, in `glossaries/`, and can be selected
before a guided translation starts.

## How the file is written

The configuration stays in the v1 format.

- **Reads never rewrite the file.** Changes use an atomic write and a revision, so two sessions cannot
  overwrite each other's changes. On a revision conflict, reload the configuration before saving again.
- **Unknown fields** allow reading with a notice, but block writes until the file is reviewed explicitly,
  so no data is lost.
- **If a write fails,** the menu says that the values only apply to the current session.
- **Reader commands** with credential options, such as `--token` or `--password`, are refused: the
  configuration is not a secret store.
- **Invalid, future or oversized files** are preserved. Keep a private copy and fix the file explicitly
  before saving again; the menu never replaces them with the default configuration.

See the [preferences contract](../architecture/modules/preferences.md) for limits, error results,
compatibility and permissions.
