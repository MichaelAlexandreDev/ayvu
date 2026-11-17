# ADR: LLM prompt, batching and structured-output contract

Status: Proposed
Decision-Date: `2026-11-17`
Owners: Project maintainer (acceptance); Providers (contract ownership)
Issue: [#375 — Add a provider-neutral LLM translation core](https://github.com/MichaelAlexandreDev/ayvu/issues/375)

Decision: extends the [provider contracts](provider-contracts.md) (`DEC-008`) and
[capabilities](provider-capabilities.md) for language-model providers; part of the AI providers series
([#100](https://github.com/MichaelAlexandreDev/ayvu/issues/100)).

## Context

LibreTranslate translates one plain-text segment per call. A language model is better used with several
segments per request, shared instructions and a structured answer, and it can damage markup or placeholders.
Without a shared core, every adapter (Ollama, Anthropic, OpenAI) would reimplement prompting, batching and
validation, and each could get the cache identity subtly wrong.

## Decision

One provider-neutral core, `ayvu.providers.llm`, sits behind the existing rich request and result. A model
adapter implements only `ModelPort.complete(ModelRequest) -> ModelResponse` and declares its transport facts.

| Concern | Contract |
| --- | --- |
| Prompt | A `PromptTemplate` with an integer `revision` and fixed instruction text. Book text, context and glossary travel as JSON data in the user message, never spliced into instructions. |
| Prompt identity | `OpaqueSemanticRevision("ayvu", "llm_prompt", revision, "llm-prompt-v<revision>")`, backed by the snapshot `llm_prompt:<sha256>` of the template, the schema and the envelope, framed by the tag `ayvu.llm_prompt`. Editing the text without bumping the revision still changes the fingerprint. |
| Batching | Units of one document and one language pair, in order, closed before `max_units` or an estimated `token_budget` (characters divided by `chars_per_token`, rounded up) would be passed. A unit is never split; an oversized one goes alone. |
| Structured output | `{"translations": [{"id": str, "text": str}]}`, nothing else. Results map to segment ids, never to positions. A missing, extra or duplicate id, invalid JSON, anything outside the schema or a truncated answer fails the batch, which is retried once per unit, alone. |
| Validation | Each unit passes the existing strict check (`protected_content_is_intact`: placeholders once and in order, protected terms, no stray markers). An empty result fails as `empty_result`; text equal to the source across languages succeeds with the warning `untranslated_result`. The source's outer whitespace is restored. |
| Glossary | Active rules (`translate`, `preserve`, `forbidden`) are a hint in the prompt. The pipeline still applies the glossary after translation, including to cached text. Because the hint changes raw output, the plan marks it native, with a terminology revision bound to the glossary reference. |
| Cache identity (D9) | `prompt_strategy = llm_structured_batch`, the prompt revision, and generation parameters for the batch policy, decoding settings, core version and output-schema revision. Retry counts and waits are operational and excluded. |
| Failures | Existing families only: timeout, connection and 5xx are `provider_transient`; 429 is `provider_rate_limited`, waited out within bounds when it carries a retry-after, otherwise returned with `retry_after_ms`; a refusal or safety stop is `provider_refusal`, non-retryable; invalid or truncated structure is `provider_output_invalid` or `provider_output_truncated`. |
| Capabilities | The core claims only what its code does (structured output, batch, context, native glossary as a hint, pre-request cancellation). Model identity, network class, retention, authentication and the other transport facts must each be stated by the adapter. |

The core performs no I/O and holds no vendor, network or credential; it is registered as application code,
so the boundary checker refuses an HTTP client or a socket in it. Composition (`composition.builders.llm`)
turns its plain semantics into the plan's values and binds it to the rich runtime.

## Consequences and limits

- Changing the template, the schema, the batch policy or a decoding value invalidates reuse for that
  setting, by design. The default LibreTranslate path, with or without a model label, is unchanged.
- The rich pipeline still calls a provider once per chunk, so in the runtime a batch holds one unit.
  `execute_batch` is the contract for multi-unit requests; feeding it several units of a document is a
  pipeline change left to a later issue.
- Token usage is not recorded: `ProviderUsage` has no token fields yet.
- The rich runtime's parallel path still requires the prepared translator factory, so a bound LLM with more
  than one worker fails with `worker_factory_unavailable` until an adapter issue bounds its parallelism.
- No CLI flag selects the core yet; the first adapter ([#376](https://github.com/MichaelAlexandreDev/ayvu/issues/376)) adds selection.

## Operational contract

The [module contract](../modules/providers.md#llm-core) holds the operational rules and the enforcing tests.
