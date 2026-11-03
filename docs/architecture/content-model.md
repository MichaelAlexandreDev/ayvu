# ADR: Content documents and translation units

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-005`, scoped to the model, identity and serialization of content documents

Related issue: [#140 — Define the minimal content document and translation unit model](https://github.com/MichaelAlexandreDev/ayvu/issues/140)

## Decision and scope

`ayvu.content` defines immutable `ContentDocument` and `TranslationUnit` values that a
format adapter produces and a pipeline consumes. The core knows identity, order,
context, protected parts and constraints. It does not know what a DOM node, a subtitle
cue or a page region is: format detail stays in an opaque locator only the adapter
can interpret.

**Nothing uses this yet.** No parser, cache, review or translation path changes,
nothing is persisted, and current `c####-s####` review IDs and the cache key are
untouched. [#142](https://github.com/MichaelAlexandreDev/ayvu/issues/142) and
[#143](https://github.com/MichaelAlexandreDev/ayvu/issues/143) adopt it.

A universal document tree, target locale and translation or review state are not part
of a source unit, and are not accepted here.

## Identity

| ID | Computed from | Changes when |
| --- | --- | --- |
| `document_id` | Project, adapter namespace, logical key | Another project, adapter or logical document |
| `revision_id` | Document, adapter and segmenter versions, document locale, every unit's fingerprint | Anything relevant in the document changes |
| `unit_id` | Document, revision, adapter identity and versions, locator | The revision or the locator changes |
| `source_digest` | Project, source text | The exact text changes |

A unit's fingerprint covers its locator, source digest, kind, order key, locale,
protected parts, constraints and context references. Hash inputs are framed as a JSON
array with a domain label and version, so fields cannot run together.

These follow from the rules above, and they are the costly parts to change later:

- **Equal text at two locators gets two IDs.** Text alone is never identity.
- **Editing one character changes every unit ID in that document,** and leaves other
  documents alone. Translations and reviews do not carry over by themselves. Continuity
  across revisions needs a later, explicit remapping decision with evidence, and no fuzzy
  match reuses an ID.
- **The revision comes from extracted content,** not from a file hash or the caller.
  `source_fingerprint` and extensions are recorded outside identity, so rewriting a
  container without changing what was extracted keeps every ID.
- **`document_id` survives an adapter upgrade,** while the revision and unit IDs change.
- **Collisions and duplicates fail.** Duplicate locators, duplicate order keys and ID
  collisions are never repaired with an encounter-order suffix.

Constructing a `ContentDocument` recomputes every ID, so a document with a wrong ID cannot
exist, at the cost of hashing twice when building. `build_document` is all or nothing.

## The unit

| Field | Rules |
| --- | --- |
| `locator` | Schema, schema version and an opaque value, unique within the document |
| `source_text` | Exact, including surrounding whitespace, and never normalized; NFC and NFD forms are different text. Whitespace alone is accepted; empty text and lone surrogates are refused |
| `kind` | Known semantic kind and version |
| `order_key` | Tuple of up to 8 non-negative integers, unique in the document. It alone decides reading order, so input order does not matter |
| `source_locale` | Explicit, `inherited` from the document or `unknown`. A document cannot be `inherited` |
| `protected_parts` | Ordered, non-overlapping half-open spans of code points, independent from provider chunks |
| `constraints` | Known names and versions, with typed values |
| `context` | Relation and the locator of another unit in the same document, resolved to `context_links` holding unit IDs. Missing and self references fail |
| `extensions` | Optional and opaque. Stored verbatim, outside identity, and never read by the core |

**Versions.** Semantic kinds, constraints, protected kinds, context relations, locator
schemas and the top-level format carry explicit versions or closed lists. Anything
unknown raises `ContentVersionError`, and so does a required extension, because none
exists yet. Unknown optional extensions round-trip unchanged. A new format may need a
new kind, which is a deliberate code change.

## Serialization

`encode_document` produces one canonical byte string per document, and `decode_document`
rebuilds everything and accepts only that exact output. Tampered IDs, digests or text and
unknown fields all raise `invalid_document`. Test fixtures pin the bytes, so a change to
the identity algorithm or format fails visibly and must become a new version.

**The encoded form holds the book's text.** It is a private project artifact and belongs
in the [artifact store](job-artifacts.md), never in logs or diagnostics.

## Privacy

IDs, `repr` and errors contain no source text, locator value, path or credential. Errors
carry a bounded code and, where it applies, a unit position. Locators and logical keys
refuse absolute paths, URLs and control characters.

**Limits the format cannot enforce:**

- **IDs and digests are salted, not keyed.** They include the project UUID, so equal text
  in two projects does not correlate. The project has no secret key, so someone who knows
  the UUID, the logical key and the locator could confirm a guess about a very short
  document. IDs must leave a project only after remapping, as the
  [threat model](product-threat-model.md) already requires for diagnostics. A per-project
  key would close this, but needs a manifest change and is left to a separate decision.
- **The source digest is not a public ID** and no proof that content is safe to log. It is
  for divergence and integrity checks only.
- **A locator value is opaque.** A locale, locator or logical key can still hold a word
  from a book when an adapter puts it there, as `sk-secret-token` has the shape of a valid
  locale. Adapters must build them from structure, which remains a review rule. Locale
  values are visible in `repr`, unlike the others.

## Limits

- At most 262144 units per document, 1 MiB of characters per unit, 256 MiB per encoded
  document, 64 entries per list and 64 KiB per extension.
- The model has no caller until #142 and #143, which are its first real test.
