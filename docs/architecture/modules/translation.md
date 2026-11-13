# Translation pipeline

The accepted scope and limits are recorded in the [decision](../decisions/translation-pipeline.md).

The contract preserves the implementation stages recorded in #142, #153, #252,
#162 and #242. Historical restrictions describe their respective integration
stages; later integrations do not revise those descriptions here.

The CLI accepts at most 100 EPUBs in a sequential batch. Without an explicit
source language it resolves each book's metadata independently. Each completed item saves
a Markdown report, and cache-only misses also save an automatically named
missing-text file; a multi-input request cannot use `--missing-output`. Duplicate
EPUB destinations are rejected before the batch starts; common-mode conflict
decisions are collected before processing. Dry runs still save the applicable
automatic reports and missing-text files, but do not publish translated EPUBs.

`TranslateBatch` orchestrates items in order through injected planning and item
execution ports. `ayvu.composition.builders.translate_batch` supplies a reusable production
composition that binds each item to its execution and the batch cancellation
controller. Normal executions delegate to `TranslateProject`; dry runs use the
existing project-free path. The batch does not run items concurrently. The
`TranslateProjectCommand.accepted_output` carries the accepted plan and must
match the command's output; its revision is checked before execution and at
publication.

`BatchCancellationController` binds a fresh `CancellationSource` to each active
job. An interruption returns the aggregate of started items and prevents the next
item from starting. Depending on when it occurs, the active job may finish as
`CANCELLED`, `FAILED` with `interrupted`, or `PARTIALLY_COMPLETED` with
`interrupted_after_publication`.
A pre-execution refusal does not print an empty batch summary.

A `report_ref` identifies the job result rendered as a report and is exposed only
when its Markdown was saved. If saving fails, the item is `FAILED` with
`report_failed`; `job_state` and `output_published` retain the actual job and
publication facts, while successful artifact and report references are absent.
Exhaustion of the bounded automatic-name search is recorded as an item failure,
while the planner continues reserving names for the remaining items.

Path identity uses existing filesystem metadata and read-only case-sensitivity
checks. These checks and the final operation are separate: a concurrent change
can occur between the last check and `os.replace` or exclusive auxiliary creation,
including replacement of a directory. They do not provide an absolute guarantee.

Both runtimes share the report values and conversions exported by `ayvu.translation`,
implemented in `translation/report.py`: `TranslationReport`, `HtmlTranslationStats`,
`TextTranslationResult`, `to_html_stats` and `to_report`. Book facts are read through
the public I/O adapter `ayvu.formats.epub.reader` (`read_package`); the values `BookFacts` and
`BookPackage` come from `ayvu.formats`. The report API loads no I/O adapter.

There are two paths through the same stages:

| Path | Entry | Identity | Durable state |
| --- | --- | --- | --- |
| Legacy | `translate_text`, through `LegacyTextPipelineFacade` | None | None beyond the v1 cache |
| Rich | `DocumentTranslator` | Real project, job, document and unit | Artifact, provenance and events |

**Production explicitly selects the rich path since #252.** Add
`"translator_models": {"libretranslate": "meu-modelo-2026-11"}` to the existing `config.json`.
The label accepts `[a-z0-9][a-z0-9_-]{0,47}` and enters identity as
`declared:<label>`, declared by the user; `unknown` remains ineligible.
**Changing the server model without changing the label reuses old results.**
There is no model discovery or new HTTP request. Without a label, `translate_text`
retains signature, results, messages and v1 key. Unlabeled previews stay on v1; labeled previews and dry runs use the transient routes below.
See [runtime selection](translation-project.md#explicit-runtime-selection-252).

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.content.protection` | Domain | Protected terms and placeholders, and checking that they survived |
| `ayvu.translation.plan` | Domain | The resolved plan, stage identities and fingerprints |
| `ayvu.translation.pipeline` | Application | The stages, their ports and the legacy facade |
| `ayvu.translation.document` | Application | Document admission, active unit execution, events and publication |
| `ayvu.translation.document_contracts` | Application | Document contracts, safe points and protected content |
| `ayvu.translation.raw_results` | Application | Canonical raw results, reuse dimensions and payload validation |
| `ayvu.translation.document_replay` | Application | Proving and replaying current and ancestor raw results |
| `ayvu.translation.document_provenance` | Application | Document production and replay lineage |
| `ayvu.translation.document_bindings` | Application | Provider, cache and memory bindings for the current unit and plan |
| `ayvu.translation.engine` | Application | One book through a format adapter and a unit executor, under the current flow's rules |
| `ayvu.v1.translation` | Format | The executor over `translate_text` and the v1 cache, and the current report's types |
| `ayvu.formats.epub.html_structure` | Format | Traversal, durable units and markup-safe reconstruction |
| `ayvu.v1.html_translate` | Format | The compatibility facade, and `translate_text` adapting the current types to it |

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

`tests/v1/test_translate_text_characterization.py` states these, and it was written and passing
against the code before this extraction.

## The engine

`TranslationEngine` reads a book through the format adapter, hands each unit to an executor and gives the results
back to the adapter. It parses no markup and calls no provider. Composition chooses between:

| Explicit rich runtime | Legacy runtime without label |
| --- | --- |
| Stops the document at the first failure | A failed text stays in the source language and the run goes on |
| V2 cache in a separate sibling file, with full identity | Reuses existing v1 cache |
| Resume reuses the previous job's proven raw results (#242) | [Resume](recovery.md) re-executes on v1 |

`ayvu.v1.translation` is the named compatibility
path to the v1 cache, and it adds no provider, model or pipeline identity to it. The units have real identity, because
extraction needs a real project.

**The current flow is the oracle.** `tests/v1/test_translation_engine_parity.py` runs both over the same books and compares
the bytes, the order of provider calls, the cache, the report, the progress and the review rows.
`tests/v1/test_translation_engine_publication.py` and `tests/v1/test_translation_engine_workers.py`
cover publication and worker behavior.
[Format compatibility](formats-compatibility.md#differences-settled-by-145) lists the differences that are deliberate.
`texts_skipped` is left at zero: only the current report object holds it, and nothing shows it.

[`TranslateProject`](translation-project.md) wraps the engine in a durable job.

Production uses the engine inside `TranslateProject` in both runtimes. In rich,
`TranslationResources.prepare_document` runs `DocumentTranslator` before the engine
consumes units; a failure there stops the book. Unlabeled previews and dry runs retain the
predecessor project-free flow. Labeled previews and dry runs use `TransientTranslation`.

## Online transient previews (REF-18 G2)

`_BookRequest` carries the translator name/model mapping and an explicit preview marker.
`composition.cli.prepare_preview` selects before stores or provider preparation. Only a valid
label chooses late online preparation; the unlabeled path retains v1 timing and observables.
The real session performs authorized discovery and supplies the execution's rate limiter.
The resulting plan uses the declared producer and observed route/capability fingerprint.

`TransientUnits` shares `LinguisticStages` with the offline operation and binds a `TransientProvider`
that checks every result against the real plan. `TransientCache` first reads an authorized exact
v2 key when an existing matching manifest grants scope, then checks the invocation overlay.
It stores the observed raw provider value before glossary application; no v1/v2 write, cache
initialization, lock, sidecar or persistent provenance is available. The overlay is cleared
and the session closed on success, failure and cancellation. Without a matching project,
there are zero persistent entry reads. Subsequent invocations may translate again.

Formats supplies v1 document enumeration, with `max_documents` before chapter selection,
`max(0, N)`, `None` for all, and metadata outside the document count. The CLI default stays 12.
`TransientTranslation` sends a `PreviewRequest` to `TransientPublication` only after all selected
units succeed. The local composition adapter owns staging, final EPUB validation, source/alias
checks and replace; Translation implements neither ZIP nor rename. Unselected members and
previews with no results keep their original member bytes. Provider failure leaves the destination
intact. Preview publishes no Markdown, project/job, lease, checkpoint, raw-results or review package.
The persistent-cache and partial-output differences were accepted on 2026-11-13 (G2).

## Offline transient dry runs (REF-18 G3)

`composition.cli` chooses `transient_dry_run` only when `selected_model` returns a valid label,
before preparation or stores. `prepare_translation` receives that modality explicitly and calls
the local preflight, which validates languages, glossary, endpoint, timeout and retry settings without
DNS, transport, a provider/session or the v1 write probe. Labeled previews use the online transient route below; durable cache-only has its separate route.

`composition/builders/translate_transient.py` reads `ProjectJsonRepository.read_catalog`, calls
`select_project`, and revalidates the single selected manifest with `load_read_only`. An absent
catalog or no matching intention grants no persistent cache scope. An unreadable/incomplete catalog,
ambiguous match or changed source fails explicitly. No project, job, lease, checkpoint, raw result or
provenance is created. The invocation-only UUID used for extraction cannot authorize persistent reads.

`TransientTranslation` extracts through Formats and processes documents in order without the normal
engine or any reconstruction/publication port. `TransientUnits` shares `LinguisticStages`, protection
and semantic identity through `LocalCachePlanResolver`. Each unit resolves its own canonical plan,
then performs an exact lookup through `ReadOnlyExactCache` at `v2_cache_path(--cache)`.
A resolved lookup hit counts as cache reuse and receives the current non-native glossary without
rewriting its raw entry. Miss, ambiguous or unsupported evidence counts as work to do. Corruption,
invalid stored protection tokens and candidate overflow fail the whole invocation.

Typed cache-only takes precedence over the dry-run classification: exact hits are hits; every other
ordinary result preserves the readable original as missing, without fuzzy, TM or artifact replay.
The existing report converters and missing-text/Markdown renderers remain in use. Only this rich
dry-run branch evaluates full-cache by `texts_missing > 0`; complete coverage exits 0 without an EPUB.
Configured workers remain unchanged, including high; documents currently run sequentially. Progress,
terminal reports and Markdown explicitly state both the configured count and sequential local analysis.
These cache/product differences were accepted on 2026-11-13 (G3 decision row).

## The legacy facade

`LegacyTextPipelineFacade` has no document, project or job identity, by design. It creates no
artifact, provenance record or replay key, and it never derives an ID from text or order.

The v1 cache and memory reach the CLI pipeline only through ports. `_LegacyV1Cache` in
`ayvu.v1.html_translate` is the named compatibility adapter for that behavior; a rich plan cannot select it.
The standalone cache has a v2 boundary and repository; CLI runs without a declared model stay on the characterized v1 adapter.
[`formats.epub.html_structure`](../decisions/html-structure.md) has neither cache nor glossary.

`MAX_PROTECTED_RECOVERY_CALLS` remains a setting of `ayvu.v1.html_translate`, passed on each call.

## The resolved plan

`LocalCachePlanResolver` resolves per-unit plans from authorized canonical cache evidence and local intent without discovery; labeled transient dry runs and durable cache-only use it in production. Each unit resolves independently; an internal proven pin narrows compatible evidence without fallback.

`resolve_plan` is pure, and it yields no plan unless the required capabilities are plainly accepted.
Its artifact and cache identities split at the glossary:

| Change | Raw artifact fingerprint | V2 semantic cache fingerprint | Post-processing fingerprint |
| --- | --- | --- | --- |
| Producer, route, capabilities, chunk limit, policy, context | Changes | Changes when cache-eligible | |
| Any stage version up to `raw_cache_write` | Changes | Changes | |
| Glossary | Changes only if the provider received it natively | Changes only with a native terminology revision | Changes |
| `post_processing` version | | | Changes |

The v2 identity also includes per-hop provider/model/adapter revisions, effective configuration and
capability fingerprints, prompt/context strategy revisions, generation parameters, segmentation, chunking,
protection, normalization and applicable format constraints. Each exact key hashes the original unit text,
including its outer whitespace and protected markup; that digest stays outside the semantic fingerprint.
Low-entropy prompt, context and native terminology inputs require an owner-issued revision tied to a durable
snapshot. Missing critical provider, model or pipeline identity makes a plan cache-ineligible. The current
rich provider boundary resolves one provider identity across every route hop; a plan cannot invent different
per-hop providers before an execution boundary resolves them.

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

**Ports.** `ExactCachePort` takes an exact entry key with authorized scope, cached-value kind, semantic
fingerprint and source-content digest. The digest stays outside the semantic fingerprint. Pinned cache-only
first resolves its opaque boundary reference against local durable identity data in the port, checks the
resolved scope and semantics, then performs exact lookups; an unresolved reference is a miss. Compatible
resolution enumerates bounded local identities. Neither port operation may discover a provider or use
transport. Writes use atomic `put_if_absent`; a same-key value or provenance conflict is typed and never
replaces the winner. `MemoryPort` receives scope, kind and semantic fingerprint but no exact source digest.
It cannot return a legacy candidate, and the pipeline checks each candidate's identity again. Fuzzy reuse is
neither approval nor corpus consent.

Identity v2 uses a closed projection and canonical UTF-8 payload with a 64 KiB ceiling, at most 8 route
hops, 256 entries per named mapping/collection and 1,024 UTF-8 bytes per opaque scalar. Ordered route and
stage sequences retain their order; mappings are sorted by canonical key. Changing field semantics or a
ceiling requires a new identity version. Canonical integers are bounded by `±(2^63 - 1)`; chunk and stage
versions are positive. Canonical decimals have magnitude at most `1,000,000,000` and at most 18 fractional
digits. Fingerprints, keys, scopes and derived hashes are sensitive
correlators and stay out of repr, events, logs, errors and diagnostics.

Cache-only has two modes: `pinned` resolves one durable execution identity reference locally and performs exact lookup;
`resolve-compatible` carries no pin and enumerates at most 256 local identities in the authorized scope.
Zero candidates miss, one is checked against the current semantic identity and more than one distinct
identity is ambiguous. Duplicates for one exact key are corruption. Neither mode uses discovery or
transport. A rich result reports incomplete units when misses are allowed; it publishes no raw-results
artifact for an incomplete document. The CLI keeps its partial EPUB and `--require-full-cache` contracts. With a model label,
[`CacheOnlyDocumentExecutor`](../../../src/ayvu/translation/cache_only_execution.py) prepares the
entire document before unit consumption, so corruption cannot be swallowed by legacy unit tolerance.
It shares the typed lookup/protection/glossary kernel in `TransientUnits`. Hit lineage uses
`reuse_exact_cache`, `SourceKind.CACHE` and a `cache_entry:` reference derived from the complete
validated exact key; it never records provider production or stores new raw-results. The offline
executor accepts no TM, fuzzy, replay, artifact publisher or provider port. Missing originals use
the injected Formats review projection for body HTML and expanded originals for title/alt.
Empty missing originals are rejected. Scope denial, candidate overflow and corrupt storage are fatal.
The engine gates full-cache gaps before reconstruction and preserves previous output; partial
allowed output keeps source for missing units and uses the existing TXT renderer, including batch.


`SQLiteExactCache` implements the port for the rich path after `prepare_cache` runs before workers.
In rich, the CLI prepares `<--cache name>.v2.sqlite` before the provider, without opening or
changing v1. The first rich use retranslates texts available only in v1. It requires
`--workers 1` and stops at the first provider failure
without publishing a partial EPUB. The job remains resumable and completed segments stay in cache.
The glossary is applied to raw v2 values. Removing the label returns to legacy on new
runs; resume between different runtimes is refused and names `legacy/v1` and `rich/v2`.
The declaration stays in configuration and operational provenance/identity, outside repr,
events, logs and checkpoints. A continuation reuses the previous job's proven raw results (#242).
Migration outside this production selection preserves `translations` as `legacy-v1`, without promoting its rows to exact entries. See
[standalone cache v2](knowledge-cache.md) for locking, recovery and limits.

## Replay

If the semantic fingerprint, document revision and job all match, only post-processing runs. The artifact's
digest and size are checked, then its content is compared with the document unit by unit.

| Found | Result |
| --- | --- |
| Valid artifact | Replay, with no cache lookup and no provider call |
| Other plan, revision or job | Normal execution |
| Missing, partial or corrupt file | The record is withdrawn, its file stays, and the work is redone |
| Valid digest but content that does not match | Fails closed with `invalid_raw_results` |

Another job replays only when it is a continuation and every recorded dimension is proved equal; see
[replay through a continuation](recovery.md#replay-through-a-continuation-242). Payload version 2 records those
dimensions; a version 1 payload still replays in its own job but proves nothing to a continuation. A job's lease must
be live.
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

## Enforced contracts

- Stage order and resolved identity: [pipeline](../../../tests/translation/test_translation_pipeline.py),
  [plan](../../../tests/translation/test_translation_plan.py), and
  [legacy characterization](../../../tests/v1/test_translate_text_characterization.py).
- Rich admission, execution and protected content:
  [identity](../../../tests/translation/test_translation_document_identity.py),
  [execution](../../../tests/translation/test_translation_document_execution.py),
  [protection](../../../tests/translation/test_translation_document_protection.py),
  [provider adapter](../../../tests/translation/test_translation_document_provider_adapter.py), and
  [cache and memory](../../../tests/translation/test_translation_document_cache_memory.py).
- Cache identity and offline reuse: [identity](../../../tests/translation/test_translation_cache_v2_identity.py),
  [cache-only](../../../tests/translation/test_translation_cache_v2_cache_only.py), and
  [fuzzy reuse](../../../tests/translation/test_translation_cache_v2_fuzzy.py).
- Raw-result validation, replay and lineage: [document replay](../../../tests/translation/test_translation_document_replay.py)
  and [continuations](../../../tests/translation/test_raw_results_resume.py).
- Engine compatibility: [parity](../../../tests/v1/test_translation_engine_parity.py),
  [publication](../../../tests/v1/test_translation_engine_publication.py), and
  [workers](../../../tests/v1/test_translation_engine_workers.py).
- Conservative retrieval: [policy](../../../tests/translation/stages/test_memory_retrieval_policy.py),
  [queries](../../../tests/translation/stages/test_memory_retrieval_queries.py), and
  [pipeline](../../../tests/translation/stages/test_memory_retrieval_pipeline.py).

### Ordered durable cache-only documents (REF-18 G8, batch 4)

`TranslationResources.documents` replaces rich `prepare_document` callbacks with a
complete document execution port. `DocumentWorkResult` contains per-unit executions,
translations, stats, statuses and bounded local warnings. `DocumentScheduler` admits
at most N documents, including finished results awaiting ordered consumption. Units
inside each document stay sequential. Book enumeration supplies the work index;
metadata occupies index zero and retains its separate title-error policy. Selected
chapter callbacks retain their selected ordinal.

The coordinator consumes document/unit/local-event order and alone calls observers,
saves checkpoints, emits global `_Run` sequences and reconstructs the EPUB. A rich
document error stops the book even without fail-fast. Cancellation, factory failure
and definitive lease loss stop admission; queued tasks are cancelled and started
tasks are joined before resources or lease are released. Acknowledged cancellation
keeps `_Run._fail` precedence over concurrent failure. Complete durable artifacts
remain resumable independently of unconsumed callbacks; partial documents receive
no completion credit. Cache-only creates no new raw-results and never replays them.

`rich_workers.py` owns resource composition; `engine.py` keeps the unchanged v1
parallel pool. High and overrides preserve the effective N only in durable offline
execution in this batch. Online concurrency, shared rate limiting and duplicate-key
arbitration remain batch 5; governed memory with multiple workers remains refused.
