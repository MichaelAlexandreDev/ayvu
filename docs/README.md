# Ayvu documentation

Start with the [README](../README.md) for what Ayvu is and a quick start. This page indexes the rest.

## User guide

| Page | What it covers |
| --- | --- |
| [Installation and setup](guide/installation.md) | Requirements, installing with uv or pip, the optional desktop extra, and running a LibreTranslate server |
| [Translating books](guide/translating.md) | Guided and developer modes, `translate` and its options, batches, output location, dry runs, the final report, text extraction, chapter selection, metadata and alt text, review packages, previews and language routes |
| [Performance and request control](guide/performance.md) | Performance profiles, workers, rate limits and retries |
| [Glossary](guide/glossary.md) | Glossary files, the guided glossary flow and how terms are applied |
| [Cache, resume and cache-only](guide/cache-and-resume.md) | The translation cache and its administration, resuming interrupted books, and rebuilding from the cache |
| [Translation memory](guide/translation-memory.md) | Reusing similar sentences, thresholds and suggestions |
| [Library](guide/library.md) | Listing originals and translations and opening them in a reader |
| [Model label, rich runtime and v2 cache](guide/rich-runtime.md) | Declaring the model label, durable jobs, the v2 cache, approved memory and failure behaviour |
| [Translating with Ollama](guide/ollama.md) | Translating with a local language model: setup, model identity, declared languages, workers, privacy and limits |
| [Configuration](guide/configuration.md) | The configuration file, its fields, profiles and limits |
| [Usage, cost and the cost cap](guide/usage-and-cost.md) | Tokens and cost in the reports, `--max-cost`, the dry-run estimate and price overrides |
| [HTTP network policy](guide/network-policy.md) | `--network-policy none` and `loopback`, and what each one blocks |

A walkthrough of both modes, step by step, is in the
[tutorial](tutorial-modo-comum-e-dev.md) (Portuguese; an English version is planned).

## For developers

- [Architecture guide](architecture/README.md): packages, module contracts, cross-cutting rules and
  decision records.
- [Contributing](../CONTRIBUTING.md): workflow and pull requests.
- [Rules for AI agents and contributors](../AGENTS.md): architecture rules, validation and the working agreement.
- [Release workflow](release-workflow.md): issues, branches and releases.
