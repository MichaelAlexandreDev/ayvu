# ADR: Translation provider contracts and registry

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-008`, scoped to the request, result and failure contracts, the registry and the LibreTranslate compatibility adapter

Related issue: [#141 — Introduce versioned translation provider contracts and registry](https://github.com/MichaelAlexandreDev/ayvu/issues/141)

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
| `ayvu.provider` | Domain | Request, result, features, routes, usage and allowlisted diagnostics |
| `ayvu.provider_service` | Application | Provider and factory ports, registry, feature decisions and strict result checking |
| `ayvu.provider_libretranslate` | Provider adapter | The compatibility adapter and its factory |

Failures and warnings reuse `JobFailure` and `JobWarning`, the producer identity reuses
`ProducerIdentity`, and spans and constraints reuse the [content model](content-model.md),
so there is one taxonomy of each.

## Request and features

A request has a schema version, request ID, optional idempotency key, the exact text,
locales, optional [content unit](content-model.md) reference, optional expected route,
opaque policy and configuration references, optional cancellation token and a set of
features.

Each optional feature (`protected_parts`, `context`, `constraints`, `deadline`) states
whether it is `required` or `advisory`. `decide_features` compares them with what a
provider can honor: a required one that it cannot honor must fail before any I/O, and an
advisory one it ignores becomes a warning such as `ignored_context`. A field a provider
ignored is never presented as honored.

## Result

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

`provider_service.execute` runs a provider so that nothing it does can pass as a
translation. A value of the wrong type, a different request ID, a conflicting route and
a raised exception all become typed failures. `KeyboardInterrupt` still propagates.

## Registry

`ProviderRegistry` is filled explicitly at a composition root and imports nothing, so
user data cannot cause code to load, and no domain or application code names a provider.
`unknown_instance`, `duplicate_instance`, `incompatible_adapter`,
`incompatible_configuration`, `factory_failed` and `cleanup_failed` are distinct errors.

`open(instance_id)` creates a provider for one block and closes it however the block
ends. It implies no singleton. A cleanup failure is reported only when the block itself
succeeded, so it never hides the block's error.

## LibreTranslate compatibility adapter

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

## Privacy

A request necessarily holds the book's text. `repr`, errors and the
`request_diagnostics` and `result_diagnostics` projections are allowlists holding no text,
context, content identifier, reference or provider payload. Failures are classified from
the typed attributes on `TranslatorError`, whose message is never read, because it can
quote up to 300 characters of a response body or a URL holding credentials. Those messages
still reach the current CLI as before, and
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159) owns that sanitization.

Credentials and policies are opaque references that nothing here resolves. There is no
JSON form of a request yet, because nothing crosses a process boundary.

## Consequences and limits

- The contracts have no production caller until later issues, so
  [#142](https://github.com/MichaelAlexandreDev/ayvu/issues/142) and
  [#128](https://github.com/MichaelAlexandreDev/ayvu/issues/128) are their first real test.
- Translation contracts now depend on the job failure and warning values.
- [Provider capabilities](provider-capabilities.md) state thread safety and concurrency
  limits. The registry is meant to be filled before concurrent use and has no lock.
