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
| `map_review_package` | The v2 review exchange | Durable units become validated `UnitTranslation` reconstruction inputs |
| `extract_visible_text`, `translate_html` | The current translation, through `ayvu.html_translate` | None |
| `apply_reviewed_html` | Legacy positional helper | None; v1 apply is rejected before the helper is called |

The v2 mapper produces the durable values accepted by `reconstruct_html`, but
the CLI does not call the mapper yet. `ayvu.html_translate` remains as a
compatibility facade with the same names and signatures, until
[#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153).

## The current translation

The current signature has no project or revision, so the facade uses `TransientHtmlDocument`. It shares the
traversal and the copying of original nodes, holds no locator or unit ID, and refuses to be serialized, so it
cannot pass for durable identity.

Its text still goes through the parser, as it always has, so that whitespace and repairs stay as they were. Each
token becomes an empty marker in that fragment and is then swapped for original nodes.

**This fixed three defects in translated books.** Markup used to be turned into a string and parsed outside the
document, where namespace prefixes are not declared.

| Source | Before | Now |
| --- | --- | --- |
| `<span epub:type="pagebreak">` inside a paragraph | `<span type="pagebreak">`, and `xlink:href` became `href` | Unchanged |
| A comment or processing instruction inside an inline tag | Its content became text visible to the reader | Unchanged |
| Book text that reads `__AYVU_TAG_0__` | Replaced by that run's markup | Unchanged |

Books translated earlier keep these defects until translated again, which can reuse the cache.

**Other effects:**

- A comment's content no longer appears in review text.
- A provider answer holding a character XML cannot represent no longer damages the original markup of its run.
  Such a character still damages the translated text itself: the parser may drop what follows, as before. Durable
  reconstruction refuses it with `invalid_text`.
- Text holding a token-like literal gets a different cache key, so it is translated once more. All other text keeps
  its key.
- A review file exported earlier from text with a comment inside an inline tag holds the old text, so its row may
  no longer match.

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
Beautiful Soup. It repairs malformed markup, reduces whitespace-only text to one character, reads CDATA as escaped
text and normalizes syntax, and this happens to any document it reads. Unchanged text reconstructs to exactly what
parsing and serializing give.

## Privacy

`HtmlStructureError` holds a bounded code. A locator holds only numbers. The source text is in the unit, so a
`ContentDocument` remains a private artifact, as the [content model](content-model.md) requires.

## Consequences and limits

- `SemanticKind` is `paragraph` or `alt_text`. Headings and titles are not told apart yet.
- A unit over the content model's size limit fails the extraction instead of being split.
- A local name decides whether a tag is inline or ignored, so a prefixed tag in another namespace with the same
  local name is treated the same way.
