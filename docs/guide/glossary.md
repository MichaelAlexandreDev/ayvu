# Glossary

## File format

A glossary accepts the simple format of term pairs, kept for compatibility:

```json
{
  "Game Loop": "game loop",
  "Observer": "Observer"
}
```

It also accepts explicit rules per term:

```json
{
  "Game Loop": {
    "rule": "translate",
    "translation": "game loop",
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

- **`translate`** sets a preferred translation.
- **`preserve`** keeps a standardised term in the final text.
- **`required: true`**, on `translate` or `preserve` rules, means the expected term must appear in the
  translated output.
- **`forbidden`** marks terms that must not appear in the output.

When a glossary is used, the final report counts how many `translate` and `preserve` applications
happened, including in texts that came from the cache. It also warns about required terms that are
missing and forbidden terms found in the output.

## Using a glossary

**In common mode**, run `uv run ayvu` and choose `Glossaries` to create or edit a glossary through the
guided flow. To create one, Ayvu asks for the original term, lets you enter a preferred translation or keep
the term untranslated, shows a preview, validates the content, and saves the JSON to:

```text
$XDG_CONFIG_HOME/ayvu/glossaries
```

When `XDG_CONFIG_HOME` is not set, the fallback is:

```text
~/.config/ayvu/glossaries
```

If glossaries are saved, the guided translation flow lets you choose one before confirming the EPUB output.

The guided catalog has these limits:

- up to 1,000 JSON files, each up to 1 MiB;
- up to 10,000 entries per file;
- terms up to 1,024 characters and translations up to 4,096 characters;
- the saved JSON is also limited to 1 MiB.

Files that are invalid, too large, symbolic links or files with several hard links cannot be selected:
choose another glossary, or fix the file outside the guided flow. An edit is refused if the file changed
after it was opened; open it again in that case. Glossary contents and names are private. These rules apply
to the managed catalog; explicit `--glossary` paths are still read by the existing advanced loader.

**In developer mode**, start from the example file:

```bash
cp glossary.example.json glossary.json

uv run ayvu translate book.epub \
  --output book-pt.epub \
  --glossary glossary.json
```

The local `glossary.json` and private glossaries are ignored by Git, so personal preferences and private
content are never committed.

## One glossary per translation

Each translation uses at most one active glossary: the file given in `--glossary`, the glossary associated
with the profile, or none. Ayvu does not accept several `--glossary` options in one run. This avoids silent
conflicts between files, such as a term marked for translation in one glossary and for preservation or
prohibition in another.

If the need grows, the planned direction is glossaries by rule rather than generic stacking: a file could
declare that all its terms are `translate`, `preserve` or `forbidden`, which saves repetition in large
glossaries. Even then, priority, conflict and report rules must be defined first.

## Protected terms

Before sending each segment to the translator, Ayvu protects special terms: URLs, file paths, terminal
commands, versions such as `v1.2.0`, inline code between backticks, placeholders and simple technical
identifiers. These terms are restored before the glossary is applied and before the translation is saved
to the cache.
