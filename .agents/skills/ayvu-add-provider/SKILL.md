---
name: ayvu-add-provider
description: Add or change an Ayvu translation provider (LibreTranslate, Ollama, Anthropic, OpenAI, or another) through the provider contracts, capabilities, identity, network policy and tests. Use when an issue adds a translator, an LLM adapter, or changes how a provider is called. Do not use for glossary or cache-only work.
---

# Add an Ayvu Provider

A provider is an adapter behind `ayvu.providers` contracts. Read `docs/architecture/modules/providers.md`,
the provider contracts and capabilities decisions, and the existing
`providers/libretranslate/adapter.py` before writing anything.

## Contract

- **Placement:** the adapter lives in `src/ayvu/providers/<name>/`; composition constructs it
  (`composition/builders/`), and the CLI selects it by name. Follow `$ayvu-place-code`.
- **Request and result:** implement the rich request and result of `providers.ports`, not
  `translate(text) -> str`. Results map to unit ids, never to positions.
- **Capabilities:** declare every capability honestly in `providers.capabilities`, with its evidence
  source. Examples: structured output, batch, context, native glossary, model identity, network class,
  authentication, retention claim, cancellation. Do not claim what is unproven.
- **Identity:** set a `ProducerIdentity` (provider, model, adapter, pipeline). Prefer a real model
  identity (a digest or a versioned id) over a declared label. Anything that changes an output (model,
  prompt revision, decoding settings, batching) goes into the semantic fingerprint, so the v2 cache never
  mixes outputs (D9).
- **Validation:** every result goes through the strict result check (placeholders, protected parts,
  markup). Invalid output is a failure, never silently accepted.
- **Failures:** map transport and API errors to the existing `JobFailure` families: transient
  (timeout, 5xx), rate limited (429, honour retry-after), refusal (non-retryable), invalid output,
  authentication or configuration (non-retryable). Align any SDK retries with Ayvu's retry policy so
  retries are not doubled.
- **Network:** all HTTP goes through `ayvu.network` and its policy. A local server uses loopback; a
  remote provider needs the remote egress policy, explicit consent and an allowlisted host. If an SDK
  owns its HTTP client, enforce the policy through the SDK's documented client hook and test it. Never
  add a fallback to another provider.
- **Credentials:** opaque references or the provider's standard environment resolution. Never in
  configuration, checkpoints, logs, reports, provenance, fixtures or commits.
- **Cost:** record token usage when the provider reports it.

## Tests

Use a fake transport or fake server; never a real provider in the suite. Cover:

- success, reordered results, and missing or extra ids;
- a broken placeholder, and invalid structured output;
- timeout, 5xx, 429 with retry-after, refusal, and cancellation before the request;
- identity: a model change gives a new fingerprint and no cache reuse;
- the network policy enforced on the adapter's HTTP client;
- no credential in any output.

Add an end-to-end CLI test that translates a small EPUB through the rich runtime with a fake server.

## Docs

A guide page under `docs/guide/` (setup, selection, privacy, cost) and the providers module contract.
