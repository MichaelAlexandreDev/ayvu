# Translation memory

The normal cache only reuses translations of **identical** segments. Translation memory is an optional
layer that also reuses **similar** segments, which helps with technical books, series and texts with
repeated sentences. It is **off by default** and turned on with `--translation-memory`:

```bash
uv run ayvu translate book.epub \
  --output book-pt.epub \
  --cache .cache/translations.sqlite \
  --translation-memory \
  --tm-apply-threshold 0.95 \
  --tm-suggest-threshold 0.80
```

`--translation-memory` currently requires `--workers 1`. For a parallel run, drop `--translation-memory`;
to use translation memory, keep the sequential default.

## Without a model label

Without `translator_models.libretranslate`, the memory keeps its legacy behaviour and uses the same
original/translated pairs already stored in the SQLite cache on disk. For each new segment with no exact
match, Ayvu looks for similar candidates in the same language pair and measures their similarity. Two
similarity thresholds (between 0 and 1) control what happens:

- **Similarity ≥ `--tm-apply-threshold`** (`0.95` by default): the stored translation is reused directly,
  without calling the translator. It counts as `Texts from memory` in the report.
- **Similarity in `[--tm-suggest-threshold, --tm-apply-threshold)`** (`0.80` to `0.95` by default): the
  segment is translated normally, and the similar match is recorded as a review suggestion, counted in
  `Memory suggestions`.
- **Similarity below `--tm-suggest-threshold`:** nothing changes, and the segment follows the normal
  translation flow.

For safety, memory acts **only on plain-text segments**: blocks with inline tags (such as `<em>` or `<a>`)
never reuse memory, and matches that involve markup are discarded, so another segment's formatting is never
injected. A reuse is **not** written back to the cache under the new text.

The reusable pairs are the cache's own, so the existing export format already serves the memory: use
`ayvu cache export` to save the original/translated pairs as JSON.

> **Quality risk (false positives).** Similar segments can mean different things: negations, numbers,
> names, small word swaps. The lower the apply threshold, the higher the risk of reusing a wrong
> translation. That is why memory is opt-in and uses high thresholds by default; the suggestion level
> exists so that less certain matches are reviewed instead of applied automatically. For a new or sensitive
> book, keep memory off, or use `--review-output` to check the result.

## With a model label

With a model label, `--translation-memory` uses governed approved memory instead of the v1 cache, and fuzzy
matches are suggestions unless conservative auto-apply is selected. See
[Model label, rich runtime and v2 cache](rich-runtime.md).
