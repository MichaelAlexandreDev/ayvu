# ADR: HTML structure

Status: `Accepted`

Date: 2026-11-04

Accepted: 2026-11-04 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-005`, scoped to HTML units, their locators and reconstruction

Related issue: [#143 — Isolate HTML segmentation and markup-safe reconstruction](https://github.com/MichaelAlexandreDev/ayvu/issues/143)

## Decision and scope

`ayvu.html_structure` knows where reader-visible text is in a document and how to put a translation back. It
imports no provider, cache, memory, glossary, chunker or interface.

| Entry | For | Identity |
| --- | --- | --- |
| `extract_html_document`, `reconstruct_html` | The [rich path](translation-pipeline.md) and [#144](https://github.com/MichaelAlexandreDev/ayvu/issues/144) | Durable: real project, document and revision |
| `extract_visible_text`, `translate_html`, `apply_reviewed_html` | The current translation, through `ayvu.html_translate` | None |

**Nothing in production calls the durable entry yet.** `ayvu.html_translate` remains as a compatibility facade
with the same names and signatures, until [#145](https://github.com/MichaelAlexandreDev/ayvu/issues/145).

## What becomes a unit

The rules are those of the current translation. Consecutive inline siblings form one unit, and block elements
and ignored blocks separate units. Each unit is one group of text nodes, never a flattened `get_text()` result.

| Content | Becomes |
| --- | --- |
| Text and inline tags | One unit, with tags as protected parts |
| `code`, `kbd`, `samp` inside a unit | One protected part, untranslated |
| `script`, `style`, `code`, `pre`, `kbd`, `samp`, `svg`, `math` as a block | Nothing |
| Comments, doctype, declarations and processing instructions between blocks | Nothing, and they stay in place |
| The same inside an inline tag | One protected part, restored as what it was |
| Whitespace, or only protected parts | Nothing |
| `alt` of an image, when asked for | Its own unit, after all text |
| Text that looks like a token | A protected part, so it returns unchanged |

## Locators

A locator states a node's position in the parsed tree, as `r:0.1.3+2` for two siblings or `a:0.1.3` for an
`alt`. It is opaque, and only this module reads it. It comes from structure, so it never depends on text or on
what was translated.

A locator means something only within one revision. A changed document, a new `HTML_SEGMENTER_VERSION` or a new
`HTML_LOCATOR_VERSION` is refused and is never remapped.

## Tokens

`reconstruct_html` receives text where `__AYVU_TAG_n__` stands for part `n` of that unit, which is what
`html_unit_template` and `UnitTranslationResult.tokenized_text` give. This is the format the pipeline and its
cache already use, so it did not change. A new meaning requires a new `HTML_SEGMENTER_VERSION`.

| Found | Code |
| --- | --- |
| A token with a part number that does not exist | `invented_token` |
| A part used twice | `duplicate_token` |
| A part not used | `missing_token` |
| A damaged token, such as `AYVU TAG 1` or `__AYVU_TAG_01__` | `malformed_token` |
| A closing part before its opening, or crossing another | `invalid_token_order` |

Reordering is accepted while the tags still nest, because a translation can move an emphasized word.

**Limit:** a token is a number, so one copied from another unit is not detected when that number exists here. It
restores this unit's own part and never the other unit's. Tokens salted per unit would change the cache keys.

## Reconstruction

**Translated text is never parsed.** It becomes text nodes, and markup comes back as copies of the original
nodes. So a translation holding `<script>` shows those characters to the reader, and cannot create an element,
an attribute or a URL.

**It is all or nothing.** Every check runs and every node is built before the first change:

| Check | Code |
| --- | --- |
| A result for a unit not in the document | `unknown_unit` |
| Two results for one unit | `duplicate_translation` |
| A unit with no result, unless `partial` is set | `missing_translation` |
| Source no longer yields this revision | `stale_revision` |
| Another segmenter or locator version | `incompatible_segmenter`, `incompatible_locator` |
| Characters XML cannot hold | `invalid_text` |
| Tokens, as above | As above |

`partial` is for previews, and units with no result keep their source text.

**What is preserved is the parser's reading of the document, not its bytes.** The parser is `lxml-xml` through
Beautiful Soup. It repairs malformed markup, reduces whitespace-only text to one character and normalizes syntax,
and this happens to any document it reads. Unchanged text reconstructs to exactly what parsing and serializing
give, with one exception: CDATA inside a unit becomes escaped text with the same meaning.

## Privacy

`HtmlStructureError` holds a bounded code. A locator holds only numbers. The source text is in the unit, so a
`ContentDocument` remains a private artifact, as the [content model](content-model.md) requires.

## Consequences and limits

- `SemanticKind` is `paragraph` or `alt_text`. Headings and titles are not told apart yet.
- A unit over the content model's size limit fails the extraction instead of being split.
- A local name decides whether a tag is inline or ignored, so a prefixed tag in another namespace with the same
  local name is treated the same way.
