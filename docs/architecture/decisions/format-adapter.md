# ADR: Format adapter contract

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Formats (contract ownership)
Issue: [#127 — Define the FormatAdapter port and capability descriptor](https://github.com/MichaelAlexandreDev/ayvu/issues/127)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-005`, scoped to the format port, its descriptor and its registry

## Decision and scope

Every format reaches Ayvu through one contract, which states what it can do, how faithfully, and on what
evidence. EPUB is the only format behind it, in [the EPUB adapter](epub-format.md), and `translate` reaches it through
[the use case](../modules/translation-project.md). There are no plugins, nothing is discovered, and adapters are registered explicitly at
a composition root.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.formats.contracts` | Domain | Descriptor, claims, negotiation, requests and results |
| `ayvu.formats.service` | Application | Operation ports, registry and the checks before any work |
| `ayvu.formats.local` | Infrastructure | Local files as sources, and the check that a destination is not the source |
| `ayvu.content_use_cases` | Application | Typed inspect and visible-content export orchestration |
| `ayvu.formats.markdown.writer` | Infrastructure | Safe Markdown planning and atomic publication of each local file |
| `ayvu.composition.builders.content` | Composition | Registers the EPUB adapter and supplies local content use cases |

Failures reuse `JobFailure`, content extraction for translation reuses `ContentDocument`, and cancellation reuses
`CancellationToken`. Durable translation artifacts still go through `ArtifactService`; visible Markdown exports
use a local writer port and return non-durable `PublishedFileRef` values.

[The compatibility map](../modules/formats-compatibility.md) lists where each current behavior goes.

## Consequences and limits

- #144 supplies the production EPUB adapter; #147 adds inspect and visible-content export as read-only consumers.
- Structured and visible-content extraction return all documents at once. A very large book may later need them one at a time.
- `FormatSource` is synchronous, and a safe point cannot interrupt a blocking read.
- `ContentScope` is checked against the descriptor, but nothing verifies that an adapter limits its units to the
  scopes requested.
- Fidelity is the same for preview and reconstruction, and #144 may show they need separate claims.

## Operational contract

The [module contract](../modules/formats.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #127 scope; later integrations do not expand this acceptance.
