# ADR: Content documents and translation units

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Content (contract ownership)
Issue: [#140 — Define the minimal content document and translation unit model](https://github.com/MichaelAlexandreDev/ayvu/issues/140)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-005`, scoped to the model, identity and serialization of content documents

## Decision and scope

`ayvu.content` defines immutable `ContentDocument` and `TranslationUnit` values that a
format adapter produces and a pipeline consumes. The core knows identity, order,
context, protected parts and constraints. It does not know what a DOM node, a subtitle
cue or a page region is: format detail stays in an opaque locator only the adapter
can interpret.

The v2 review exchange exposed by `ayvu.review` is the first caller of durable
units: it binds them to a project/source/artifact revision and returns
`UnitTranslation` values for structural reconstruction. It does not persist
content or replace the legacy v1 `c####-s####` export used by the CLI. The
production CLI translation path and cache key remain unchanged. Format
extraction/reconstruction adoption is described in [#143](https://github.com/MichaelAlexandreDev/ayvu/issues/143).

A universal document tree, target locale and translation or review state are not part
of a source unit, and are not accepted here.

## Limits

- At most 262144 units per document, 1 MiB of characters per unit, 256 MiB per encoded
  document, 64 entries per list and 64 KiB per extension.
- The v2 review exchange is the model's first real caller and first integration
  test; the CLI's v1 exporter and apply command are still separate legacy paths.

## Operational contract

The [module contract](../modules/content.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #140 scope; later integrations do not expand this acceptance.
