# ADR: Provider capabilities and availability

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Providers (contract ownership)
Issue: [#103 — Expose provider capabilities and operational constraints](https://github.com/MichaelAlexandreDev/ayvu/issues/103)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-008`, scoped to descriptors, capability snapshots, availability and negotiation

## Decision and scope

Facts about a provider have different lifetimes, so they are separate values:

| Value | Lifetime | Holds |
| --- | --- | --- |
| `ProviderDescriptor` | Static, per adapter version | Adapter and protocol identity, compatible versions |
| `ProviderCapabilitySnapshot` | One resolved configuration | Every capability with evidence, limits, observation time |
| `ProviderAvailability` | Expires | Whether it answers now, and the languages it reported |

Consumers decide only from these values, never from a provider name or a URL.

The v2 translation plan consumes `capability_fingerprint` for semantic cache identity.
The CLI's v1 cache and storage remain unchanged, and snapshots are not persisted. Ranking,
routing preference and automatic tuning are not accepted here, and network policy enforcement belongs to
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159). The shared discovery use
cases belong to [#128](https://github.com/MichaelAlexandreDev/ayvu/issues/128).

The values are in `ayvu.providers.capabilities`, the probe port in `ayvu.providers.service`,
and the LibreTranslate report in `ayvu.providers.libretranslate.adapter`.

## Consequences and limits

- The contract has no production caller until #128 and
  [#142](https://github.com/MichaelAlexandreDev/ayvu/issues/142).
- Availability is not cached or refreshed by anything; a caller decides when to probe.
- Only capability states are negotiated; limits are reported but not compared with a request.

## Operational contract

The [module contract](../modules/providers.md#capabilities) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #103 scope; later integrations do not expand this acceptance.
