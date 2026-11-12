# Providers

The accepted scopes and limits are recorded in the [contract decision](../decisions/provider-contracts.md)
and the [capability decision](../decisions/provider-capabilities.md).

## Contracts

This section preserves the implementation stage recorded in #141; statements about
later consumers describe that stage, not their current delivery status.

### Request and features

A request has a schema version, request ID, optional idempotency key, the exact text,
locales, optional [content unit](../decisions/content-model.md) reference, optional expected route,
opaque policy and configuration references, optional cancellation token and a set of
features.

Each optional feature (`protected_parts`, `context`, `constraints`, `deadline`) states
whether it is `required` or `advisory`. `decide_features` compares them with what a
provider can honor: a required one that it cannot honor must fail before any I/O, and an
advisory one it ignores becomes a warning such as `ignored_context`. A field a provider
ignored is never presented as honored.

### Result

| Status | Text | Failure |
| --- | --- | --- |
| `succeeded` | Required and non-empty | None |
| `failed` | None | Required, and not of family `cancelled` |
| `cancelled` | None | Required, of family `cancelled` |

Expected failures are results, not exceptions. A result also carries the effective
producer and route, warnings, usage and a delivery state:

| Delivery | Meaning for a retry |
| --- | --- |
| `not_sent` | Nothing reached the provider |
| `completed` | The provider answered, successfully or not |
| `unknown` | It may have processed the request, as after a timeout |

**Unknown is explicit.** A model the provider does not report is `unknown`, and a usage
value of `None` means not known, never zero.

`providers.service.execute` runs a provider so that nothing it does can pass as a
translation. A value of the wrong type, a different request ID, a conflicting route and
a raised exception all become typed failures. `KeyboardInterrupt` still propagates.

### Registry

`ProviderRegistry` is filled explicitly at a composition root and imports nothing, so
user data cannot cause code to load, and no domain or application code names a provider.
`unknown_instance`, `duplicate_instance`, `incompatible_adapter`,
`incompatible_configuration`, `factory_failed` and `cleanup_failed` are distinct errors.

`open(instance_id)` creates a provider for one block and closes it however the block
ends. It implies no singleton. A cleanup failure is reported only when the block itself
succeeded, so it never hides the block's error.

### LibreTranslate compatibility adapter

Payload, route and call order, retries, backoff, timeout and rate limiting stay inside
the wrapped translator. Each `create` invokes the translator factory once, so each
parallel task keeps its own HTTP session, while a rate limiter given to that factory stays
shared, as it is today.

The adapter honors no optional feature. Before any HTTP request, in this order, it fails
on a required feature, a conflicting route, an expired deadline or a cancellation:

| Situation | Code | Family | Delivery |
| --- | --- | --- | --- |
| Required feature | `unsupported_required_feature` | `capability` | `not_sent` |
| Requested route differs from the real one | `route_conflict` | `capability` | `not_sent` |
| Deadline already passed | `deadline_expired` | `resource` | `not_sent` |
| Cancellation observed | `cancelled_before_request` | `cancelled` | `not_sent` |
| Connection failure | `provider_unavailable` | `provider_transient` | `unknown` |
| Timeout | `provider_timeout` | `provider_transient` | `unknown` |
| Other transport failure | `provider_request_failed` | `provider_transient` | `unknown` |
| HTTP 401 or 403 | `provider_unauthorized` | `provider_permanent` | `completed` |
| HTTP 429 | `provider_rate_limited` | `provider_transient` | `completed` |
| HTTP 5xx | `provider_server_error` | `provider_transient` | `completed` |
| Other HTTP error | `provider_rejected` | `provider_permanent` | `completed` |
| Invalid response | `malformed_response` | `provider_permanent` | `completed` |
| Empty translation of non-blank text | `empty_result` | `provider_permanent` | `completed` |

**Limits of what it can honestly claim:**

- **Cancellation** is observed only at `provider_before_request`. A request cannot
  interrupt a blocking call, and on an intermediate route both calls run with no safe
  point between them, because the adapter wraps `RoutedTranslator`. A cancellation source
  must declare this safe point.
- **Deadline.** An expired one fails without I/O. During the call, the existing HTTP timeout
  applies, and a timeout is never reported as cancellation.
- **Retry count.** Retries happen inside the translator, so `requests` is `None`.
- **Truncation.** LibreTranslate gives no signal for it, so a truncated but non-empty
  translation cannot be detected here.

**A deliberate difference until consumers migrate:** an empty `translatedText` is a
success for `Translator.translate` and a failure for this adapter.
[#145](https://github.com/MichaelAlexandreDev/ayvu/issues/145) keeps the current translator, so
[#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153) settles it.

### Privacy

A request necessarily holds the book's text. `repr`, errors and the
`request_diagnostics` and `result_diagnostics` projections are allowlists holding no text,
context, content identifier, reference or provider payload. Failures are classified from
the typed attributes on `TranslatorError`, whose message is never read, because it can
quote up to 300 characters of a response body or a URL holding credentials. Those messages
still reach the current CLI as before, and
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159) owns that sanitization.

Credentials and policies are opaque references that nothing here resolves. There is no
JSON form of a request yet, because nothing crosses a process boundary.

## Capabilities

This section preserves the implementation stage recorded in #103, including its
v2 fingerprint amendment. Static descriptors, configured capability snapshots and
expiring availability remain separate, as recorded in the capability decision.

### Capabilities

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

### Limits

Each limit has a unit, a scope, an origin and semantics of `known`, `unknown` or `unbounded`.

**A client setting is never a discovered server limit.** A limit of origin `client_policy`
must have `configuration` evidence. A known or unbounded limit of origin `server` needs
`runtime_observation` or `provider_documentation`, and configuration is refused as evidence
for it.

### Availability

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

### Negotiation

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

### Fingerprint

`capability_fingerprint` is a deterministic input to the v2 semantic cache identity. It
covers adapter and protocol identity, the configuration reference, server limits and the
capabilities in `FINGERPRINT_CAPABILITIES`, which can
change a translation or what it exposes. Time, availability, client policy and operational
capabilities are excluded, so changing a rate limit would not change it.

### LibreTranslate

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
  [request handling](../decisions/provider-contracts.md) cannot drift apart.
- **Evidence is the adapter's code, not the server.** A server upgrade can change behavior
  without changing the snapshot. `/languages` is the only thing observed at runtime.

**The probe** calls `GET /languages` and nothing else: no `/health`, because the current
contract has none, and no translation, so no document text is sent. It inherits the
translator's timeout and retries, so a composition root bounds its cost by giving it a
translator with `retries=0`. `discover_route` derives direct and intermediate routes from
fresh evidence with the existing routing rules.

### Privacy

`repr` output and the `snapshot_projection` and `availability_projection` allowlists exclude
endpoints, credentials, configuration references, content and provider payloads. The probe
classifies failures from attributes and never reads an error message. Direct callers of
`list_languages` keep their current strings, which can quote a response body or a URL.
Replacing those belongs to a later consumer migration.

## Discovery

This contract implements the service half of
[#128](https://github.com/MichaelAlexandreDev/ayvu/issues/128), under the shared application
contracts of `DEC-017` and the [provider contracts](../decisions/provider-contracts.md) of `DEC-008`. It does
not accept `DEC-015`: nothing here evaluates network policy.

`ProviderDiscoveryService` is what interfaces call to learn about providers, instead of reaching
one directly. `languages`, `test-translator`, guided language selection and the provider part of
translation preflight all use it. Translation errors now omit URLs, response bodies and raw
transport exceptions under #159, while retaining typed connection/timeout/protocol categories.

`composition.cli` (#158) delegates to `ayvu.composition.builders.providers`, which derives the
operation-bound decisions from the same immutable `none`/`loopback` snapshot enforced
by the translator. Missing selection explicitly records the loopback compatibility
default. See the [HTTP policy contract](../network-policy.md). The shared use cases
still do not evaluate URLs or duplicate policy classification.

No code here names a provider or reads a URL. Adapters are registered explicitly at a composition
root and implement `ProviderDiscoveryAdapter`.

### Use cases

| Use case | Network | Result |
| --- | --- | --- |
| `list_providers` | No | Configured instances, in a fixed order |
| `describe` | No | Identity, capabilities, last availability and eligibility, kept separate |
| `availability` | Yes, unless a fresh result is reused | State, freshness and reason |
| `languages` | Yes | Display list with names |
| `resolve_route` | Yes | Direct, intermediate or no route |
| `test_provider` | Yes | The result of sending the synthetic text |
| `preflight` | Yes | Whether translation can start, and on which route |

Each result has an outcome that keeps these apart: unknown instance, policy denied, unavailable,
degraded, unknown, no route, unsupported, indeterminate, cancelled, deadline expired and failed.

**`preflight` sends no translation.** It uses the same evidence as discovery. An unsupported
requirement is rejected from static facts, with no I/O and before the network decision is checked.
Unknown or configuration-dependent requirements yield `indeterminate`, never implicit support.

**`test_provider` is the only operation that sends text**, the known `Hello world`, and only because
a person asked for that test.

**Freshness** is `fresh`, `cached` or `unknown`. `availability` reuses earlier evidence only if asked
to and only before it expires, and `describe` reports expired evidence as unknown.

### Network decision

Every network operation takes a `ResolvedProviderNetworkDecision`, made elsewhere. It holds allow or
deny, a class from the [threat model](../product-threat-model.md), a reason code, an optional policy
reference, a policy revision, an issuer, validity times and a binding to one instance, configuration
and operation.

The service **enforces and does not evaluate**. A request can leave only after these checks, each with
its own code and with zero provider I/O on failure:

| Check | Code |
| --- | --- |
| Present | `network_decision_missing` |
| Well formed | `network_decision_malformed` |
| From a trusted issuer | `network_decision_untrusted` |
| Already valid | `network_decision_not_yet_valid` |
| Not expired | `network_decision_expired` |
| Bound to this instance, configuration and operation | `network_decision_binding_mismatch` |
| At the current policy revision | `network_decision_revision_mismatch` |
| Allowed | `network_decision_denied` |

The class `unclassified` records that no policy was evaluated, so that nothing has to guess a class
from an address. #159 now supplies real policy decisions through the existing contract,
including bounded actionable denial codes, without recreating discovery DTOs or use cases.

### Cancellation and deadlines

Both are checked once, immediately before a request. A request cannot interrupt a blocking call, so a
source must declare `discovery_before_request`, and a deadline bounds only whether a call starts.

### Privacy

Language names are server text kept only for display: they are absent from availability evidence and
from `repr`. Results hold bounded codes, and adapter exceptions are reduced to a code without being
read, because their messages can quote a URL or a response body.

### LibreTranslate

`LibreTranslateDiscoveryAdapter` wraps the existing translator. Its evidence comes only from
`GET /languages`: there is no `/health` and no translation request. An empty list is `degraded`
with `no_languages`, and a malformed or oversized list is `degraded` as well.

Retries stay inside the translator, as they do today.

### What the CLI shows

Exit codes, flags, summaries, the language table and the success line are unchanged. What changed:

| Where | Before | Now |
| --- | --- | --- |
| Technical detail, developer mode only | The message, with URL and up to 300 characters of the server's response | A code such as `provider_timeout` |
| Warning in guided selection | The same message | The code |
| Next step in preflight | Included the URL, which can hold credentials | No URL |
| `test-translator` receiving an empty translation | `Translator OK` with exit 0, a false positive | Failure with exit 1 |
| `test-translator` with an invalid language code | Sent to the server | Refused before sending |
| `test-translator --help` | No mention | States that it sends `Hello world` |

**What this gives up:** the server's own explanation, such as `bad language pair`, is no longer shown in
developer mode. The code still separates rejected, unauthorized, rate-limited and server errors.

**The hidden probe is gone.** Preflight had a fallback that translated `Hello world` when a translator
could not list languages. The real translator always can, so this reached only test doubles. A translator
without discovery now gets an unknown route and no request.

## Enforced contracts

- [test_libretranslate_capabilities](../../../tests/providers/libretranslate/test_libretranslate_capabilities.py).
- [test_provider_discovery_libretranslate](../../../tests/providers/libretranslate/test_provider_discovery_libretranslate.py).
- [test_provider_libretranslate_factory](../../../tests/providers/libretranslate/test_provider_libretranslate_factory.py).
- [test_provider_libretranslate_translation](../../../tests/providers/libretranslate/test_provider_libretranslate_translation.py).
- [test_translator_http](../../../tests/providers/libretranslate/test_translator_http.py).
- [test_translator_languages](../../../tests/providers/libretranslate/test_translator_languages.py).
- [test_translator_rate_limit](../../../tests/providers/libretranslate/test_translator_rate_limit.py).
- [test_translator_routes](../../../tests/providers/libretranslate/test_translator_routes.py).
- [test_provider](../../../tests/providers/test_provider.py).
- [test_provider_capabilities_fingerprint](../../../tests/providers/test_provider_capabilities_fingerprint.py).
- [test_provider_capabilities_model](../../../tests/providers/test_provider_capabilities_model.py).
- [test_provider_capabilities_negotiation](../../../tests/providers/test_provider_capabilities_negotiation.py).
- [test_provider_discovery_authorization](../../../tests/providers/test_provider_discovery_authorization.py).
- [test_provider_discovery_operations](../../../tests/providers/test_provider_discovery_operations.py).
- [test_provider_discovery_preflight](../../../tests/providers/test_provider_discovery_preflight.py).
- [test_provider_service](../../../tests/providers/test_provider_service.py).
