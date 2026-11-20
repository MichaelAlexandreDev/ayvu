# ADR: EPUB format adapter

Status: Accepted
Decision-Date: `2026-11-04`
Owners: Project maintainer (acceptance); Formats (contract ownership)
Issue: [#144 — Implement the production EPUB FormatAdapter](https://github.com/MichaelAlexandreDev/ayvu/issues/144)

Accepted: 2026-11-04 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-005`, scoped to the production EPUB adapter and its archive limits

## Decision and scope

EPUB is the first real format behind the [format contract](format-adapter.md). **`translate` and the guided
translation use it**, through [the use case](../modules/translation-project.md). Dry run and preview use the transient routes; `translate_epub` was removed with the v1 runtime in REF-18 step 6.
`inspect` and `extract` now use the shared read-only
[content use cases](../modules/content-use-cases.md), and the review application rejects
legacy v1 review data before EPUB access; v2 apply is orchestrated by the shared
review use case and delegates reconstruction and validation to this adapter.

| Module | Responsibility |
| --- | --- |
| `ayvu.formats.epub.archive` | Reads the container against limits, copies an EPUB changing only what it is told to, and holds the OPF title edit the current flow also uses |
| `ayvu.formats.epub.adapter` | The descriptor and ports of the format contract |
| `ayvu.formats.epub.html_structure` | Every XHTML document, so there is [no second segmenter](html-structure.md) |

It imports no provider, cache, glossary or interface, even indirectly, and a test imports it alone to prove that.

For visible-content export, the adapter returns one record per XHTML item in the current manifest order. It derives
the output stem from the internal item name and delegates reader-visible body text selection to
`formats.epub.html_structure.extract_body_visible_text`; the Markdown writer does not parse EPUB or HTML. The CLI keeps the
existing `NNN-stem.md` names and text formatting. The old `formats.epub.io.extract_markdown` helper remains as a legacy
facade, but `inspect` and `extract` no longer call it.

## Consequences and limits

- The adapter is stricter than the current flow, and [the compatibility map](../modules/formats-compatibility.md) lists what #145 has to settle.
- A document is parsed in memory, so the limit on one document is also the limit on one chapter.
- Extraction returns all documents at once. This has been tested on small books only.
- Nothing in `ayvu.formats.epub.adapter` limits how long a single member takes to read, because a safe point cannot interrupt a read.

## Operational contract

The [module contract](../modules/formats-epub.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #144 scope; later integrations do not expand this acceptance.
