# ADR: HTML structure

Status: Accepted
Decision-Date: `2026-11-04`
Owners: Project maintainer (acceptance); Formats (contract ownership)
Issue: [#143 — Isolate HTML segmentation and markup-safe reconstruction](https://github.com/MichaelAlexandreDev/ayvu/issues/143)

Accepted: 2026-11-04 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-005`, scoped to HTML units, their locators and reconstruction

## Decision and scope

`ayvu.formats.epub.html_structure` knows where reader-visible text is in a document and how to put a translation back. It
imports no provider, cache, memory, glossary, chunker or interface.

| Entry | For | Identity |
| --- | --- | --- |
| `extract_html_document`, `reconstruct_html` | The [rich path](translation-pipeline.md) and [#144](https://github.com/MichaelAlexandreDev/ayvu/issues/144) | Durable: real project, document and revision |
| `map_review_package` | The v2 review exchange | Durable units become validated `UnitTranslation` reconstruction inputs |
| `extract_visible_text`, `translate_html` | The current translation, through `ayvu.v1.html_translate` | None |
| `apply_reviewed_html` | Legacy positional helper | None; v1 apply is rejected before the helper is called |

The v2 mapper produces the durable values accepted by `reconstruct_html`, but
the CLI does not call the mapper yet. `ayvu.v1.html_translate` remains as a
compatibility facade with the same names and signatures, until
[#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153).

## Consequences and limits

- `SemanticKind` is `paragraph` or `alt_text`. Headings and titles are not told apart yet.
- A unit over the content model's size limit fails the extraction instead of being split.
- A local name decides whether a tag is inline or ignored, so a prefixed tag in another namespace with the same
  local name is treated the same way.

## Operational contract

The [module contract](../modules/formats-html.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #143 scope; later integrations do not expand this acceptance.
