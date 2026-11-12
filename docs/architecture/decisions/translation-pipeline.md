# ADR: Translation pipeline

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Translation (contract ownership)
Issue: [#142 — Extract a versioned TranslationUnit pipeline](https://github.com/MichaelAlexandreDev/ayvu/issues/142)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-007`, scoped to extracting the linguistic sequence from the HTML module

## Decision and scope

The linguistic work on one text is a fixed sequence that knows nothing about documents.
HTML traversal and markup reconstruction are in [`ayvu.formats.epub.html_structure`](html-structure.md).

## Consequences and limits

- User-facing messages raised by the legacy path stay in Portuguese, as they were. Application code
  holding interface text is debt to settle with the rich path in
  [#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153). The use case stores only a code.
- `LinguisticOutcome.glossary_usage` is opaque to the pipeline, since the application layer cannot
  import the glossary module.
- The facade is built for each text, which adds a small allocation per call.

## Operational contract

The [module contract](../modules/translation.md) contains the pipeline, runtime,
replay, cancellation and provenance rules and their enforcement evidence.
Historical implementation descriptions preserve the accepted #142 scope and the
later #153, #252, #162 and #242 integration stages; they do not change this acceptance.
