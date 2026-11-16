# Architecture guide

Start here. The documents are grouped by what they are, not by when they were written.

## Where to start

- **Adding or moving code:** read [where new code goes](cross-cutting/where-code-goes.md), then the [package-structure decision](decisions/package-structure.md), the rules every module follows.
- **How a module works:** open its contract in [`modules/`](#modules); the decision that created it is linked from there.
- **Security and boundaries:** the [product threat model](cross-cutting/product-threat-model.md) and [threat register](cross-cutting/threat-register.md), the [network policy](cross-cutting/network-policy.md), and the [module and technical boundaries](cross-cutting/boundaries.md).
- **Proposals:** a decision with `Status: Proposed` is not accepted, and its order in this list does not accept it.

## Decisions

Every record opens with `Status`, `Decision-Date`, `Owners` and `Issue`. A companion record carries its parent's header and is part of the same decision.

| Record | Status |
| --- | --- |
| [ADR: Desktop technology evaluation](decisions/adr-desktop-stack.md) | Accepted |
| [ADR: Lifecycle and IPC of trusted local workers — DESKTOP-136](decisions/adr-local-worker-isolation.md) | Accepted |
| [ADR: Native credential storage selection (DESKTOP-129)](decisions/adr-secret-store-adapters.md) | Proposed |
| [Assistance provenance, transitions and adversarial review](decisions/ai-assistance-authority-transitions.md) | Proposed |
| [Bounded roles and authority of optional assistance](decisions/ai-assistance-authority.md) | Proposed |
| [ADR: Content documents and translation units](decisions/content-model.md) | Accepted |
| [ADR: EPUB format adapter](decisions/epub-format.md) | Accepted |
| [ADR: Format adapter contract](decisions/format-adapter.md) | Accepted |
| [ADR: HTML structure](decisions/html-structure.md) | Accepted |
| [ADR: Job checkpoints and derived artifacts](decisions/job-artifacts.md) | Accepted |
| [ADR: Durable translation job lifecycle](decisions/job-lifecycle.md) | Accepted |
| [ADR: Local project SQLite storage and migration locations](decisions/local-project-storage.md) | Accepted |
| [Modular-monolith boundary decision](decisions/modular-monolith-boundaries.md) | Accepted |
| [Package structure and dependency rules](decisions/package-structure.md) | Accepted |
| [Product threat assessment decision](decisions/product-threat-model.md) | Accepted |
| [ADR: Durable project identity and standalone CLI resolution](decisions/project-lifecycle.md) | Accepted |
| [ADR: Provenance records and lineage](decisions/provenance.md) | Accepted |
| [ADR: Provider capabilities and availability](decisions/provider-capabilities.md) | Accepted |
| [ADR: Translation provider contracts and registry](decisions/provider-contracts.md) | Accepted |
| [Rights governance examples and acceptance gate](decisions/rights-purpose-examples.md) | Proposed |
| [Rights, purpose, retention and revocation governance](decisions/rights-purpose-governance.md) | Proposed |
| [Rights assertions, purpose grants and effective policy](decisions/rights-purpose-grants.md) | Proposed |
| [Revocation, retention and tombstones](decisions/rights-purpose-retention.md) | Proposed |
| [ADR: Translation pipeline](decisions/translation-pipeline.md) | Accepted |
| [Retirement of the v1 translation runtime](decisions/v1-retirement.md) | Accepted |
| [Compatibility matrix of the v1 retirement](decisions/v1-retirement-matrix.md) | Accepted |

## Modules

The operating contracts of each module.

| Module contract |
| --- |
| [Artifacts](modules/artifacts.md) |
| [Content inspection and export](modules/content-use-cases.md) |
| [Content](modules/content.md) |
| [Format compatibility map](modules/formats-compatibility.md) |
| [EPUB format](modules/formats-epub.md) |
| [HTML structure](modules/formats-html.md) |
| [Format contracts](modules/formats.md) |
| [Shared application use-case matrix](modules/interfaces-cli.md) |
| [Jobs](modules/jobs.md) |
| [Cache storage and administration](modules/knowledge-cache.md) |
| [Managed glossary catalog](modules/knowledge-glossary.md) |
| [Approved translation memory](modules/knowledge-memory.md) |
| [Library discovery and external opening](modules/library.md) |
| [Global preference fields, as data (read by the preferences tests)](modules/preferences-fields.json) |
| [Global preferences](modules/preferences.md) |
| [Project translation profiles](modules/projects-profiles.md) |
| [Project storage and migrations](modules/projects-storage.md) |
| [Projects](modules/projects.md) |
| [Provenance](modules/provenance.md) |
| [Providers](modules/providers.md) |
| [Recovery](modules/recovery.md) |
| [Review exchange and application](modules/review.md) |
| [Translation context](modules/translation-context.md) |
| [Project translation](modules/translation-project.md) |
| [Translation pipeline](modules/translation.md) |

## Cross-cutting

Rules and models that span modules, and the refactoring's evidence and maps.

| Document |
| --- |
| [Module and technical boundaries](cross-cutting/boundaries.md) |
| [CLI option boundary and surface snapshot](cross-cutting/cli-surface.md) |
| [Static architectural dependency checks](cross-cutting/dependency-enforcement.md) |
| [Module ownership, flows and composition](cross-cutting/module-ownership.md) |
| [Per-run HTTP policy (#159)](cross-cutting/network-policy.md) |
| [Product security, privacy, and data flows](cross-cutting/product-threat-model.md) |
| [Refactoring baseline](cross-cutting/refactoring-baseline.md) |
| [Refactoring file map](cross-cutting/refactoring-map.md) |
| [Product threat register](cross-cutting/threat-register.md) |
| [Where new code goes](cross-cutting/where-code-goes.md) |
