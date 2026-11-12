# Modular-monolith boundary decision

Status: Accepted
Decision-Date: `2026-09-03`
Owners: Project maintainer (acceptance, recorded as DevStarrk1137)
Issue: [#125 — DESKTOP-004](https://github.com/DevStarrk1137/ayvu/issues/125)

Accepted: 2026-09-03 by `DevStarrk1137` (project maintainer)

## Status and scope

This record establishes the accepted logical ownership and dependency rules for
evolving Ayvu as a local-first modular monolith. Acceptance does not move
production code, enforce imports, add a Desktop framework, or freeze the
illustrative physical package layout. Those changes remain owned by separately
reviewed issues.

The current product is defined by the [product scope](../../product-scope.md), its
observable behavior is protected by the
[translation workflow migration baseline](../../translation-workflow-migration-baseline.md),
and its interface-neutral operations are catalogued in the
[shared application use-case matrix](../modules/interfaces-cli.md). This baseline
organizes those operations without changing them.

## Context

Ayvu currently has a flat `src/ayvu` package. The modules are focused enough to
be testable, but orchestration, presentation, policies, and concrete adapters
still meet directly in several places:

- `interfaces/cli/app.py` parses input and renders output while also constructing collaborators
  and coordinating long workflows;
- `formats/epub/io.py` preserves the EPUB archive while also coordinating translation,
  review, and memory collaborators;
- `v1/html_translate.py` combines markup-safe reconstruction with translation
  stages such as cache, chunking, glossary, and translation memory;
- SQLite, JSON, HTTP, subprocess, parser, and terminal types are exposed by
  modules that will eventually implement narrower ports.

Adding another interface directly to these modules would reproduce orchestration
and error handling. Moving files into new directories without first defining
ownership would only hide the coupling. Ayvu therefore needs logical boundaries
that can be introduced one use case at a time while the CLI remains operational.

## Decision drivers and invariants

This decision is driven by the following constraints:

1. CLI, future Desktop, local API, and MCP adapters must invoke the same
   application use cases instead of duplicating business rules.
2. The original EPUB remains immutable, and every preview, translation, review,
   or export is a distinct artifact.
3. Visible-text filtering, markup reconstruction, cache semantics, glossary
   ordering, resume behavior, and archive preservation must survive migration.
4. Domain and application contracts must remain usable without Typer, Rich,
   Qt, requests, ebooklib, Beautiful Soup, SQLite, or an MCP SDK.
5. Filesystem, network, parser, process, persistence, and secret-bearing
   boundaries require explicit ports and policy-aware adapters.
6. Proposed Desktop, provider, format, project, job, and AI concepts must not be
   represented as delivered capabilities.
7. Registries, plugin APIs, and universal format models must not precede the
   concrete adapters that prove their contracts. A second adapter is introduced
   only as a bounded vertical after its dependency gates, and becomes evidence
   for the smallest shared abstraction.

This acceptance records the modular-monolith, inward-dependency, and shared
application-contract decisions represented by `DEC-001`, `DEC-002`, and
`DEC-017`. It does not accept any other proposed architecture decision. Later
scoped acceptances live in their own ADRs: [project lifecycle](project-lifecycle.md)
(`DEC-003`), [local storage](local-project-storage.md) (`DEC-012`) and
[job lifecycle](job-lifecycle.md) (`DEC-004`) and [job artifacts](job-artifacts.md)
(extending `DEC-004` and `DEC-012`), [provenance](provenance.md) (`DEC-014`) and
[content model](content-model.md), [format adapter](format-adapter.md) and [EPUB format](epub-format.md) (`DEC-005`) and
[provider contracts](provider-contracts.md) and
[provider capabilities](provider-capabilities.md) (`DEC-008`) and
[translation pipeline](translation-pipeline.md) (`DEC-007`).

The [local workers and IPC](adr-local-worker-isolation.md) decision (#167) was
`Accepted` on 2026-11-10. There is still no production runner, and it does not change
these accepted boundaries.

## Where the contracts live

The architecture dimensions, dependency direction and technical contract boundaries are in [module and technical boundaries](../cross-cutting/boundaries.md); module ownership, flows, composition roots, the migration sequence, fitness functions and the public evidence are in [module ownership, flows and composition](../cross-cutting/module-ownership.md).

## Consequences

### Benefits

- interfaces can evolve without duplicating core workflows;
- risky filesystem, network, parser, database, and process boundaries become
  independently testable;
- current modules can migrate incrementally instead of through a high-risk
  rewrite;
- format and provider evolution are decoupled;
- structured outcomes can support CLI, future Desktop, and automation without
  leaking framework types.

### Costs and tradeoffs

- temporary adapters and explicit seams add code during migration;
- some current files will have split ownership until a relevant use case moves;
- contracts require discipline to avoid becoming speculative or overly broad;
- accepted rules still need later automated enforcement and ongoing review;
- cross-module changes may require more deliberate transaction and error design.

## Alternatives rejected for this decision

| Alternative | Reason rejected |
| --- | --- |
| Keep orchestration in the CLI and call it from other interfaces | Couples future interfaces to Typer/Rich behavior and duplicates presentation decisions |
| Create the full target directory tree immediately | Produces empty or speculative packages without proving seams through real use cases |
| Use a global service locator or shared `utils` module | Hides ownership and permits dependencies to bypass explicit ports |
| Let formats call providers directly | Couples EPUB/HTML preservation to translation-engine selection and blocks independent evolution |
| Build a plugin registry before a second adapter | Freezes an unproven abstraction and expands the security surface prematurely |
| Split into processes or services now | Adds deployment and failure complexity without evidence that a modular in-process boundary is insufficient |

## Acceptance record

The project maintainer explicitly accepted this decision on 2026-09-03 after
review and merge of [PR #180](https://github.com/DevStarrk1137/ayvu/pull/180).
The acceptance covers the logical modular-monolith boundaries, inward dependency
direction, module ownership, shared application services, and incremental
migration rules in this record, with no amendments.

Acceptance does not make illustrative package names stable import paths, select
a Desktop toolkit, or accept unrelated architecture decisions. Issue
[#133](https://github.com/DevStarrk1137/ayvu/issues/133) owns automated
enforcement. Every implementation issue must still revalidate the repository,
its dependencies, and the compatibility baseline before changing code.
