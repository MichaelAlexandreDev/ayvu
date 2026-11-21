# Ollama provider

The first model adapter on the [LLM core](providers.md#llm-core), from
[#376](https://github.com/MichaelAlexandreDev/ayvu/issues/376). It follows the
[prompt and batching decision](../decisions/llm-prompt-batching.md) (`Proposed`) and the
[HTTP policy contract](../cross-cutting/network-policy.md). The user-facing page is
[Translating with Ollama](../../guide/ollama.md).

## Modules

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.providers.ollama.client` | Provider | HTTP through `ayvu.network`: `GET /api/tags` lists models with their digests, `POST /api/chat` asks one. Typed `OllamaError` reasons, retries of connection failures and 5xx within configured delays, a shared rate limiter |
| `ayvu.providers.ollama.chat` | Provider | `ModelPort` over `/api/chat`: one non-streamed call with the core's JSON schema as `format`, decoding values as `options`, the answer mapped back to a `ModelResponse` |
| `ayvu.providers.ollama.discovery` | Provider | Transport facts, the producer and snapshot, `find_model`, validated `discover_model_names`, the declared languages and the discovery adapter behind `test-translator` and `languages` |
| `ayvu.composition.builders.ollama` | Composition | The preflight that refuses what Ollama cannot run and discovers the model before any book work, the `LlmBinding`, and the CLI discovery session |

`composition.builders.rich_provider` selects Ollama by translator name (`selected_model`, `model_selection`,
`translation_preflight`, `prepare_model_translation`); `composition.cli` stays the only root.

## Identity (D9)

- **Producer:** `ProducerIdentity("ollama", "<name>@<digest>", "ollama_chat@1", "llm_core@1")`. The model
  comes from `provenance.observed_model`, which accepts a name as Ollama reports it and a bare lowercase
  sha256; it is disjoint from `declared:` labels, which keep their own rule.
- **Capability fingerprint:** the snapshot's configuration reference is `ollama_model:<digest>`, so the
  digest also enters the capability fingerprint. Client limits (timeout, rate, workers) stay out of it.
- **Semantic fingerprint:** the core's prompt revision, batch policy, decoding settings, core version and
  schema revision, as for every LLM adapter. A digest change therefore gives a new fingerprint and no
  cache reuse; LibreTranslate identities and cache keys are unchanged. A golden value pins the Ollama
  capability fingerprint in `test_persisted_identities`.

## Before any book work

The CLI's admission rule (`rich_provider.require_model`) asks `identity_source` where a translator's identity
comes from. LibreTranslate needs a declared label; Ollama's identity is discovered, so it is admitted without
one, and the checks below refuse what it cannot run. An unknown translator keeps its own error.

`ollama_preflight` runs before the job, because the job records its producer when it starts:

| Situation | Result |
| --- | --- |
| `--preview` or `--cache-only` | Refused: not available with Ollama yet |
| `--dry-run` | An estimate on this machine that never reaches this preflight ([usage](providers-usage.md)) |
| No `translator_models.ollama` | Refused: names the setting to add |
| More workers than the configured parallelism (default 1) | Refused before any request |
| A pair outside the declared languages | Refused before any request |
| `--network-policy none` or a non-loopback URL | `PreflightPolicyError`, before any socket |
| Unreadable EPUB, glossary or cache | The established preflight messages, before discovery |
| Server unreachable, or the model not installed | Refused after `GET /api/tags` alone |

The result carries the glossary, the bound `LlmBinding` and an empty translator factory: each worker's
model, with its own client and HTTP session, comes from the binding, and the rate limiter is shared.

## Transport

| Ollama answer | Core failure |
| --- | --- |
| Timeout | `provider_timeout`, transient, delivery unknown; not retried by the client |
| Connection failure or 5xx | Retried within the delays from `--retries`; then `provider_unavailable` or `provider_server_error` |
| 429 | Left to the core, which honours `Retry-After` within its bounds |
| 3xx | `provider_redirect_refused`, never followed |
| A body without `done`, a message or a known `done_reason` | `malformed_response` |
| `done_reason: length` | `provider_output_truncated` after the per-unit retry |
| Content outside the schema | `provider_output_invalid` after the per-unit retry |

Cancellation is observed at `provider_before_request`, so a cancelled unit sends nothing.

## Selection (#377)

`composition.builders.selection` owns flag/profile/configuration precedence and the job-bound selection
on resume. Its named `composition.cli` factories are the only interface entry points. Settings discovery
uses `discover_model_names` through `composition.builders.ollama`, with policy authorization and a closed
client; invalid names or transport failures return bounded codes and allow manual validated input.
Durable resume reuses the original model name from provenance and refuses changed installed digests.

## Languages and discovery

A model has no fixed language list, so the adapter declares seventeen languages; `LANGUAGE_DISCOVERY`
and `ROUTE_DISCOVERY` are unsupported and every declared pair is a direct route. `observe` calls
`/api/tags` only: no document text is sent, and a selected model that is missing degrades availability
with `model_not_installed`. `test-translator --translator ollama` sends the synthetic `Hello world` through
the core.

## Limits

- Token counts are reported by Ollama but not recorded: `ProviderUsage` has no token fields yet.
- The parallelism is a composition setting, not yet a user setting; selection in the guided menu,
  profiles and resume is implemented by `composition.builders.selection` (#377).
- With `--translator ollama`, the CLI's LibreTranslate default URL means `http://localhost:11434`.

## Enforced contracts

- [test_client](../../../tests/providers/ollama/test_client.py), [test_chat](../../../tests/providers/ollama/test_chat.py)
  and [test_discovery](../../../tests/providers/ollama/test_discovery.py).
- [test_ollama_binding](../../../tests/composition/test_ollama_binding.py) and
  [test_cli_ollama](../../../tests/interfaces/cli/test_cli_ollama.py).
- [test_provenance_observed_model](../../../tests/provenance/test_provenance_observed_model.py) and
  [test_ollama_model_entry](../../../tests/preferences/test_ollama_model_entry.py).
