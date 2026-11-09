# ADR: Provider capabilities and availability

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: extends `DEC-008`, scoped to descriptors, capability snapshots, availability and negotiation

Related issue: [#103 — Expose provider capabilities and operational constraints](https://github.com/MichaelAlexandreDev/ayvu/issues/103)

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

The values are in `ayvu.provider_capabilities`, the probe port in `ayvu.provider_service`,
and the LibreTranslate report in `ayvu.provider_libretranslate`.

## Capabilities

A snapshot must state every member of `Capability` exactly once, so absence cannot imply
support. A new capability is therefore a code change, which every adapter then has to
answer for.

| State | Required evidence |
| --- | --- |
| `supported`, `unsupported` | Any source except `none` |
| `unknown` | `none` |
| `configuration_dependent` | `configuration` |

A claim that breaks this table is refused as contradictory.

**Runtime unavailability is not a capability.** It only ever changes
`ProviderAvailability`, so a failed probe cannot rewrite a descriptor or a snapshot.

## Limits

Each limit has a unit, a scope, an origin and semantics of `known`, `unknown` or `unbounded`.

**A client setting is never a discovered server limit.** A limit of origin `client_policy`
must have `configuration` evidence. A known or unbounded limit of origin `server` needs
`runtime_observation` or `provider_documentation`, and configuration is refused as evidence
for it.

## Availability

| Situation | State |
| --- | --- |
| Valid language list | `available` |
| Empty list, malformed response or too much data | `degraded` |
| Timeout, no connection or HTTP error | `unavailable` |
| Never probed, or a broken probe | `unknown` |
| Evidence past its expiry | `unknown`, with reason `evidence_expired` |

Evidence expires exactly at `expires_at`, and expired evidence means neither available nor
unavailable. Only language codes are kept, at most 512 languages with 512 targets each.
Names and other server text are dropped.

## Negotiation

`negotiate` is pure, so it performs no I/O.

| Required capability, or availability | Decision |
| --- | --- |
| Any capability `unsupported` | `reject` |
| Any capability `unknown` or `configuration_dependent` | `preflight_required` |
| Availability given and not fresh and `available` | `preflight_required` |
| Otherwise | `accept` |

Only a capability rejects, because runtime state can change a moment later. Without an
availability argument, `accept` covers capabilities alone and says nothing about whether
the provider is reachable.

## Fingerprint

`capability_fingerprint` is a deterministic input to the v2 semantic cache identity. It
covers adapter and protocol identity, the configuration reference, server limits and the
capabilities in `FINGERPRINT_CAPABILITIES`, which can
change a translation or what it exposes. Time, availability, client policy and operational
capabilities are excluded, so changing a rate limit would not change it.

## LibreTranslate

| State | Capabilities |
| --- | --- |
| `supported` | Plain text input and output, language and route discovery, cancellation before the request |
| `unsupported` | Context, constraints, protected parts, deadline, batch, streaming, structured output, native glossary, multimodal input, in-flight cancellation, sharing one instance across threads |
| `unknown` | Model identity, retention, training, cost and resource needs |
| `configuration_dependent` | Network class, authentication |

- **The URL is never read** to state a capability. A loopback address does not prove offline
  execution, privacy, retention or the absence of egress, so local and remote URLs produce
  the same snapshot.
- **Server limits are all `unknown`:** maximum input, concurrency and quota. Timeout, retries
  and request rate are client policy. The rate reported is the limiter's own, because that
  is what applies when a limiter is shared.
- **The features the adapter honors derive from these facts,** so the claim and the
  [request handling](provider-contracts.md) cannot drift apart.
- **Evidence is the adapter's code, not the server.** A server upgrade can change behavior
  without changing the snapshot. `/languages` is the only thing observed at runtime.

**The probe** calls `GET /languages` and nothing else: no `/health`, because the current
contract has none, and no translation, so no document text is sent. It inherits the
translator's timeout and retries, so a composition root bounds its cost by giving it a
translator with `retries=0`. `discover_route` derives direct and intermediate routes from
fresh evidence with the existing routing rules.

## Privacy

`repr` output and the `snapshot_projection` and `availability_projection` allowlists exclude
endpoints, credentials, configuration references, content and provider payloads. The probe
classifies failures from attributes and never reads an error message. Direct callers of
`list_languages` keep their current strings, which can quote a response body or a URL.
Replacing those belongs to a later consumer migration.

## Consequences and limits

- The contract has no production caller until #128 and
  [#142](https://github.com/MichaelAlexandreDev/ayvu/issues/142).
- Availability is not cached or refreshed by anything; a caller decides when to probe.
- Only capability states are negotiated; limits are reported but not compared with a request.
