# ADR: Translation provider contracts and registry

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Providers (contract ownership)
Issue: [#141 — Introduce versioned translation provider contracts and registry](https://github.com/MichaelAlexandreDev/ayvu/issues/141)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-008`, scoped to the request, result and failure contracts, the registry and the LibreTranslate compatibility adapter

## Decision and scope

A provider receives a rich request and returns a rich result instead of
`translate(text, source, target) -> str`. The existing LibreTranslate translator is
exposed through a compatibility adapter that wraps it and changes nothing in it.

**Nothing uses this yet.** `Translator`, `RoutedTranslator`, `create_translator()`,
preflight, the HTML and EPUB code and the CLI stay as they are. The only change to
existing code adds classification attributes to `TranslatorError`, with messages byte
for byte the same. Capability descriptors, availability and negotiation are in
[provider capabilities](provider-capabilities.md), a second provider belongs to
[#100](https://github.com/MichaelAlexandreDev/ayvu/issues/100), and consumer migration
to later issues.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.providers.ports` | Domain | Request, result, features, routes, usage and allowlisted diagnostics |
| `ayvu.providers.service` | Application | Provider and factory ports, registry, feature decisions and strict result checking |
| `ayvu.providers.libretranslate.adapter` | Provider adapter | The compatibility adapter and its factory |

Failures and warnings reuse `JobFailure` and `JobWarning`, the producer identity reuses
`ProducerIdentity`, and spans and constraints reuse the [content model](content-model.md),
so there is one taxonomy of each.

## Consequences and limits

- The contracts have no production caller until later issues, so
  [#142](https://github.com/MichaelAlexandreDev/ayvu/issues/142) and
  [#128](https://github.com/MichaelAlexandreDev/ayvu/issues/128) are their first real test.
- Translation contracts now depend on the job failure and warning values.
- [Provider capabilities](provider-capabilities.md) state thread safety and concurrency
  limits. The registry is meant to be filled before concurrent use and has no lock.

## Operational contract

The [module contract](../modules/providers.md#contracts) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #141 scope; later integrations do not expand this acceptance.
