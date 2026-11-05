# Review exchange package and codec

Status: `Implemented`; shared application use cases and CLI orchestration are connected.

Related issue: [#148 — Define a markup-safe versioned review exchange contract](https://github.com/DevStarrk1137/ayvu/issues/148)

## Versions and encoding

The package schema and CSV codec have independent versions:

| Version | Value | Owns |
| --- | --- | --- |
| `schema_version` | `2` | Project, source, machine-artifact, format, document, locator, unit, text and protected-part contract |
| `codec_version` | `1` | CSV preamble, metadata spelling, columns, escaping, nulls and byte encoding |

The first CSV record is `AYVU-REVIEW-PACKAGE,2,<canonical JSON metadata>`. The
second record contains the fixed ordered columns listed in
[`review_exchange.py`](../../src/ayvu/review_exchange.py). Rows are sorted by
`(document_ref, unit_id)` on export and re-encoding. Metadata and protected-part JSON use ASCII
escaping, sorted object keys, compact separators and no non-finite numbers. CSV
uses RFC-style double-quote escaping and minimal quoting. The writer emits UTF-8
without BOM, CRLF record endings, and a final CRLF. The reader accepts LF or CRLF
record endings, preserves CR/LF inside quoted cells, and rejects bare record CR.
It also accepts what a spreadsheet does on saving: a UTF-8 byte order mark, empty
cells padding the shorter first record, blank records, and a semicolon delimiter
across the whole file. A spreadsheet that converts a cell's value, such as turning
`1.50` into a number, changes the text, so the row fails its fingerprint check.
Re-encoding normalizes accepted input to canonical bytes; row identity does not
depend on input order.

Every row has exactly these columns, in this order:

1. `document_ref`, `document_revision_id`, `locator_schema`,
   `locator_schema_version`, `locator_fingerprint`, `unit_id`, `source_digest`,
   `source_text_fingerprint`, `source_text`, `machine_translation`,
   `machine_fingerprint`, `reviewed_text`, `protected_parts`, `review_status`,
   `reviewer_note`.

The metadata header holds opaque project ID and revision, source SHA-256 and
size, complete machine-artifact identity and digest/size, format/adapter/contract
versions, and the deterministic package ID. Document references and revision
IDs are opaque `doc:v1:` and `rev:v1:` hashes. Locators are represented by their
schema/version and a digest of the adapter locator; no absolute path or locator
value is exported. Unit and source identities come from the durable content
model. Text fingerprints are SHA-256 over exact UTF-8 text, with no Unicode or
whitespace normalization.

`reviewer_note` distinguishes null from empty: raw `\\N` means null, a leading
backslash is doubled, and an empty cell means an empty note. All other text
columns preserve empty strings exactly. A blank `reviewed_text` can be parsed
for inspection but cannot produce a reconstruction plan. The review status is
one of `pending`, `reviewed`, or `rejected`; mapping requires every row to be
`reviewed`. There is no implied approval, translation-memory insertion, or
training consent.

## Spreadsheet safety and limits

The codec prefixes formula-triggering cells with the exact reversible string
`'⟪AYVU-FORMULA-ESCAPE-v1⟫`. It covers `=`, `+`, `-`, and `@` after leading
spaces, tabs, CR or LF, and conservatively escapes cells that begin with tab,
CR or LF. If user text begins with the reserved escape string, export doubles
that exact prefix. Import strips one exact prefix only. Ordinary apostrophes,
similar strings and other prefixes remain user text.

The raw-byte limit is checked before decoding. After strict UTF-8 decoding, a
bounded scanner checks CSV record width before `csv.reader` allocates a list of
cells; row, decoded-cell, total-content and metadata limits are enforced while
records are consumed:

| Budget | V2 | Legacy v1 inspection |
| --- | ---: | ---: |
| Raw file bytes | 64 MiB | 32 MiB |
| Data rows | 100,000 | 100,000 |
| Columns | 15 | 64 |
| Decoded characters per cell | 64 Ki | 64 Ki |
| Total decoded row content | 32 Mi | 16 Mi |
| Metadata characters | 16 Ki | n/a |
| Metadata cardinality/depth | 32 items / 3 levels | n/a |
| Distinct documents | 32,768 | n/a |
| Protected parts in one unit | 128 | n/a |
| Reviewer note | 8 Ki | n/a |

Path readers request at most the raw-byte limit plus one byte. UTF-8, CSV
structure, row/column counts, cell sizes, decoded content, metadata shape and
cardinality all fail with bounded code-only errors. Errors, reprs and inspection
values never include source/machine/review text, notes, raw parser detail, paths
or locator values.

## Markup-safe projection and mapping

Export creates a package ID from the immutable project/artifact/format context
and sorted document/unit/source/machine fingerprints. Each protected span is
projected to a placeholder derived from the package ID, unit ID and part index.
The `source_text` and `machine_translation` cells contain plain text plus these
placeholders; they never contain trusted markup from the importer. The
`protected_parts` JSON records each issued placeholder and the closed part kind.

Import checks the complete package against the current project, source,
machine artifact, format descriptor, documents and machine translations before
returning a plan. It rejects stale revisions, ownership or fingerprint
mismatches, duplicate/conflicting rows, missing/extra/unknown document or unit
IDs, and incompatible schema/codec/format/locator versions. The row set must
exactly cover the extracted units. Unknown documents never remain warnings on a
successful mapping.

Edited text is untrusted plain text. Only exact placeholders issued for this
package and unit can be converted to the existing structural token inputs for
`reconstruct_html`. Missing, duplicated, invented, malformed, cross-unit,
cross-package or reordered placeholders fail before a plan is returned. The
review contract conservatively requires the original placeholder order; it
does not permit moving parts even where the lower-level HTML reconstructor can
accept some nested reorderings. HTML tags, attributes, entities, URLs, paths,
spreadsheet formulas and prompt-like text have no authority. Reconstruction
restores only nodes already present in the matching `ContentDocument`.

The pure API is `export_review_package`, `encode_review_package`,
`decode_review_package`, `read_review_package`, `write_review_package` and
`map_review_package` in `ayvu.review_exchange`. Mapping returns a
`ReviewReconstructionPlan` with stable `ContentDocument` and `UnitTranslation`
values for the format reconstructor. Parsing and mapping do not persist state,
publish artifacts, write provenance/events, contact providers, reconstruct or
publish an EPUB.

## Legacy and current CLI behavior

`ayvu.review_import.read_review_csv` is a bounded v1 inspection reader. It
returns `LegacyReviewInspection` with redacted rows and duplicate-ID warnings;
it never creates stable identity. `epub_io.apply_reviewed_epub` rejects this
type with `legacy_reexport_required` before opening EPUB input or output. The
CLI `apply-review` command applies the same rule and asks for a fresh v2 package
bound to the exact durable source and machine artifact. `translate --review-output`
now delegates from its `TranslateProjectResult` to the shared review application
use case. The CLI can run side-effect-free validation with `--validate-only`, or
reconstruct and publish a new reviewed artifact. The use cases and their D106
publication protocol are documented in
[`review-application.md`](review-application.md).

## Verification

`tests/test_review_exchange.py` covers canonical bytes, encoding/version/null
rules, formula round-trip, bounds, identities/fingerprints, duplicate/status
failures, placeholder attacks, redaction, legacy refusal, production EPUB
extraction, namespaced and nested markup, and side-effect instrumentation. The
tests stage structural reconstruction explicitly and do not publish an EPUB or
call a provider.
