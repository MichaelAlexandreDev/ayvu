# ADR: Translation pipeline

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-007`, scoped to extracting the linguistic sequence from the HTML module

Related issue: [#142 — Extract a versioned TranslationUnit pipeline](https://github.com/MichaelAlexandreDev/ayvu/issues/142)

## Decision and scope

The linguistic work on one text is a fixed sequence that knows nothing about documents.
HTML traversal and markup reconstruction are in [`ayvu.html_structure`](html-structure.md).

There are two paths through the same stages:

| Path | Entry | Identity | Durable state |
| --- | --- | --- | --- |
| Legacy | `translate_text`, through `LegacyTextPipelineFacade` | None | None beyond the v1 cache |
| Rich | `DocumentTranslator` | Real project, job, document and unit | Artifact, provenance and events |

**The current translation uses only the legacy path**, so `translate_text` keeps its signature,
results, messages and v1 cache key. [#145](https://github.com/MichaelAlexandreDev/ayvu/issues/145) keeps
that path, as [the engine](#the-engine) explains, so nothing in production calls the rich path until
[#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153) gives it a cache.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.translation_protection` | Domain | Protected terms and placeholders, and checking that they survived |
| `ayvu.translation_plan` | Domain | The resolved plan, stage identities and fingerprints |
| `ayvu.translation_pipeline` | Application | The stages, their ports and the legacy facade |
| `ayvu.translation_document` | Application | The rich path: per-unit results, document artifact, replay, events and provenance |
| `ayvu.translation_engine` | Application | One book through a format adapter and a unit executor, under the current flow's rules |
| `ayvu.legacy_translation` | Format | The executor over `translate_text` and the v1 cache, and the current report's types |
| `ayvu.html_structure` | Format | Traversal, durable units and markup-safe reconstruction |
| `ayvu.html_translate` | Format | The compatibility facade, and `translate_text` adapting the current types to it |

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

## The engine

`TranslationEngine` reads a book through the format adapter, hands each unit to an executor and gives the results
back to the adapter. It parses no markup and calls no provider. It exists because the rich path cannot replace the
current translation yet:

| Rich path | What the current flow needs |
| --- | --- |
| Stops the document at the first failure | A failed text stays in the source language and the run goes on |
| No production cache until #153 | Running a book again reuses the v1 cache |
| Resuming needs #146 | The current `resume` keeps working from its file |

#153 and #146 both depend on #145, so neither can come first. `ayvu.legacy_translation` is the named compatibility
path to the v1 cache, and it adds no provider, model or pipeline identity to it. The units have real identity, because
extraction needs a real project.

**The current flow is the oracle.** `tests/test_translation_engine.py` runs both over the same books and compares
the bytes, the order of provider calls, the cache, the report, the progress and the review rows.
[Format compatibility](format-compatibility.md#differences-settled-by-145) lists the differences that are deliberate.
`texts_skipped` is left at zero: only the current report object holds it, and nothing shows it.

[`TranslateProject`](translate-project.md) wraps the engine in a durable job.

**Nothing in the current translation calls either of them yet.** Dry run and the preview stay on the current flow when
callers migrate, because they create no project and so have no identity to give the adapter.

## The legacy facade

`LegacyTextPipelineFacade` has no document, project or job identity, by design. It creates no
artifact, provenance record or replay key, and it never derives an ID from text or order.

The v1 cache and the memory reach the pipeline only through ports. `_LegacyV1Cache` in
`ayvu.html_translate` is the only adapter for the current cache, and only this facade selects it.
`html_translate` therefore still imports the cache and glossary as a compatibility facade, and those
boundary exceptions remain until #153. [`html_structure`](html-structure.md) has neither.

`MAX_PROTECTED_RECOVERY_CALLS` remains a setting of `ayvu.html_translate`, passed on each call.

## The resolved plan

`resolve_plan` is pure, and it yields no plan unless the required capabilities are plainly accepted.
Its fingerprints split at the glossary:

| Change | Raw fingerprint | Post-processing fingerprint |
| --- | --- | --- |
| Producer, route, capabilities, chunk limit, policy, context | Changes | |
| Any stage version up to `raw_cache_write` | Changes | |
| Glossary | Changes only if the provider received it natively | Changes |
| `post_processing` version | | Changes |

If what really ran differs from the plan, the document fails with `plan_invalidated` or
`result_route_conflict`, because a new plan is needed.

## The rich path

`DocumentTranslator.translate` refuses to start unless the job is `running`, belongs to the document's
project and holds a live lease owned by the caller. There is no way to run it without identity.

**Results are per unit, and persistence is per document.** This departs from the issue's original
per-unit wording, by the maintainer's decision. A single edit gives a document new unit IDs, so
replay is document-wide in practice, and per-unit storage does not fit:

| 30,000 units, measured on btrfs | Per unit | Per document, as built |
| --- | --- | --- |
| Added time | 46.8 minutes | 17.7 seconds, for 60 documents |
| Project database | 68 MiB, past the 64 MiB limit | 0.3 MiB |
| Files | 30,000 | 60, totalling 8.3 MiB |

A document that did not finish publishes nothing, so resuming inside a document repeats it. Which unit
came from where is in the artifact, which is private because it holds the book's text.

| Result source | Provider called | Published |
| --- | --- | --- |
| `exact_cache`, `applied_memory`, `provider` | Only for `provider` | Yes, when the whole document completes |
| `cache_only_missing`, `dry_run` | No | No, and the document is not complete |
| `artifact_replay` | No | Reuses the existing artifact |
| `blank` | No | Recorded with the document |

A suggestion from memory is `memory_suggested` on a `provider` result.

**Protected parts** become tag tokens, so the stages keep them out of the provider and check them the
same way as inline markup. A result holds `text`, with parts expanded, and `tokenized_text`, with tokens.
Only the second tells original markup from what a provider wrote, so reconstruction uses it. Text that looks
like a token is refused only outside a protected part. A provider failure stops the document with a sanitized failure, unlike the
legacy path, which records text errors and continues. A provider's empty answer is a failure here too.

**Ports.** `ExactCachePort` and `MemoryPort` key on the plan's raw fingerprint, which the v1 cache and
the cache-backed memory do not satisfy, so a plan cannot reach them. **There is no production cache
for this path** until [#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153).

## Replay

If the raw fingerprint, document revision and job all match, only post-processing runs. The artifact's
digest and size are checked, then its content is compared with the document unit by unit.

| Found | Result |
| --- | --- |
| Valid artifact | Replay, with no cache lookup and no provider call |
| Other plan, revision or job | Normal execution |
| Missing, partial or corrupt file | The record is withdrawn, its file stays, and the work is redone |
| Valid digest but content that does not match | Fails closed with `invalid_raw_results` |

**Limit:** only the owning job can replay, so a continuation job redoes the work. A job's lease must
also be live, so resuming after a crash needs [#146](https://github.com/MichaelAlexandreDev/ayvu/issues/146).
Because a phase cannot return in one event stream, a second run in the same stream needs its own
`progress_phase`.

## Cancellation

It is observed before each unit, between stages, between chunks and at the artifact's safe points, and a
`CancellationSource` must declare `SAFE_POINTS`. It emits the request and its acknowledgement and publishes
nothing. A call already running finishes first, and emitting the job's outcome stays with the job's owner.

## Provenance

Each document records one entry per kind of origin it used, plus `store_raw_results` holding the artifact,
or `replay_raw_results` when replaying. Memory records state an unknown producer. Publication and the records
do not commit together, so a crash between them leaves an artifact without records, and a later replay
records only itself.

## Consequences and limits

- User-facing messages raised by the legacy path stay in Portuguese, as they were. Application code
  holding interface text is debt to settle with the rich path in
  [#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153). The use case stores only a code.
- `LinguisticOutcome.glossary_usage` is opaque to the pipeline, since the application layer cannot
  import the glossary module.
- The facade is built for each text, which adds a small allocation per call.
