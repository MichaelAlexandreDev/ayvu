# ADR: Translation pipeline

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-007`, scoped to extracting the linguistic sequence from the HTML module

Related issue: [#142 — Extract a versioned TranslationUnit pipeline](https://github.com/MichaelAlexandreDev/ayvu/issues/142)

## Decision and scope

The linguistic work on one text is a fixed sequence that knows nothing about documents.
HTML traversal and markup reconstruction stay in `ayvu.html_translate`.

This document describes the first of two steps, and #142 stays open until the second:

| Step | Content | State |
| --- | --- | --- |
| Extraction | The stages and `LegacyTextPipelineFacade`, used by `translate_text` | This change |
| Rich path | Resolved plan, typed per-unit result, artifacts, replay and provenance | Pending |

**This step adds no behavior.** It moves code, and `translate_text` keeps its signature,
results, messages and v1 cache key.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.translation_protection` | Domain | Protected terms and placeholders, and checking that they survived |
| `ayvu.translation_pipeline` | Application | The stages, their ports and the legacy facade |
| `ayvu.html_translate` | Format | The DOM, and `translate_text` adapting the current types to the facade |

## Stages

`LEGACY_TEXT_STAGES` names the order, and a text that leaves early never reaches later stages:

| Stage | What it does | Can end the text as |
| --- | --- | --- |
| `unit_whitespace` | Separates outer whitespace, which is neither sent nor cached | `blank` |
| `exact_reuse` | Reads the exact cache, ignoring an entry whose protected content is damaged | `exact_cache` |
| `legacy_fuzzy_reuse` | Looks up translation memory | `applied_memory` |
| `execution_gates` | Stops for cache-only, then for dry run | `cache_only_missing`, `dry_run` |
| `protection` | Replaces terms with placeholders and splits into chunks | |
| `provider_chunks` | Sends chunks in order; if placeholders do not survive, retries with plain fragments | |
| `placeholder_restoration` | Refuses a result whose protected content did not survive | Raises |
| `raw_cache_write` | Stores the translation before any glossary change | |
| `post_processing` | Applies the glossary and restores outer whitespace | `provider` |

**Orderings that must hold:** the cache precedes memory, and applied memory is not promoted to
the cache. Cache-only and dry run never call the provider, and cache-only wins if both are set.
Reused text still receives the glossary. The raw text is cached before the glossary changes it.
Outer whitespace comes back last.

`tests/test_translate_text_characterization.py` states these, and it was written and passing
against the code before this extraction.

## The legacy facade

`LegacyTextPipelineFacade` has no document, project or job identity, by design. It creates no
artifact, provenance record or replay key, and it never derives an ID from text or order.

The v1 cache and the memory reach the pipeline only through ports. `_LegacyV1Cache` in
`ayvu.html_translate` is the only adapter for the current cache, and only this facade selects it.
`html_translate` therefore still imports the cache and glossary, and those boundary exceptions
remain until [#143](https://github.com/MichaelAlexandreDev/ayvu/issues/143).

`MAX_PROTECTED_RECOVERY_CALLS` remains a setting of `ayvu.html_translate`, passed on each call.

## Consequences and limits

- User-facing messages raised by the pipeline stay in Portuguese, as they were, because this step
  changes no behavior. Application code holding interface text is debt to settle when
  [#145](https://github.com/MichaelAlexandreDev/ayvu/issues/145) moves translation to typed results.
- `LinguisticOutcome.glossary_usage` is opaque to the pipeline, since the application layer cannot
  import the glossary module.
- The facade is built for each text, which adds a small allocation per call.
