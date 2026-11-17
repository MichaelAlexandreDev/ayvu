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

The model label lives in `translator_models`; see
[Model label, rich runtime and v2 cache](rich-runtime.md).

## Translation profiles

Profiles can be added by hand under `profiles`:

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
