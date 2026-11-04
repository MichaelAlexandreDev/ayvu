# Provider discovery use cases

This contract implements the service half of
[#128](https://github.com/MichaelAlexandreDev/ayvu/issues/128), under the shared application
contracts of `DEC-017` and the [provider contracts](provider-contracts.md) of `DEC-008`. It does
not accept `DEC-015`: nothing here evaluates network policy.

`ProviderDiscoveryService` is what interfaces call to learn about providers, instead of reaching
one directly. `languages`, `test-translator`, guided language selection and the provider part of
translation preflight all use it. Errors during translation itself do not yet: they keep their
current messages until #153 and #159. Only `--fail-fast` gained a message of its own, in #145.

`ayvu.provider_composition` assembles LibreTranslate for the CLI until
[#158](https://github.com/MichaelAlexandreDev/ayvu/issues/158) consolidates composition. Its decision
is `unclassified`, with reason `legacy_cli_unrestricted`, because the CLI has no network policy yet.
It reads no URL to claim otherwise, so this is a record and not a protection.

No code here names a provider or reads a URL. Adapters are registered explicitly at a composition
root and implement `ProviderDiscoveryAdapter`.

## Use cases

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

## Network decision

Every network operation takes a `ResolvedProviderNetworkDecision`, made elsewhere. It holds allow or
deny, a class from the [threat model](product-threat-model.md), a reason code, an optional policy
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
from an address. A real policy can later produce the same input without changing these use cases, and
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159) is its first real test.

## Cancellation and deadlines

Both are checked once, immediately before a request. A request cannot interrupt a blocking call, so a
source must declare `discovery_before_request`, and a deadline bounds only whether a call starts.

## Privacy

Language names are server text kept only for display: they are absent from availability evidence and
from `repr`. Results hold bounded codes, and adapter exceptions are reduced to a code without being
read, because their messages can quote a URL or a response body.

## LibreTranslate

`LibreTranslateDiscoveryAdapter` wraps the existing translator. Its evidence comes only from
`GET /languages`: there is no `/health` and no translation request. An empty list is `degraded`
with `no_languages`, and a malformed or oversized list is `degraded` as well.

Retries stay inside the translator, as they do today.

## What the CLI shows

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
