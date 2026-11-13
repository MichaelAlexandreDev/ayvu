# Retirement of the v1 translation runtime

Status: Accepted
Decision-Date: `2026-11-12`
Owners: the Ayvu repository owner and maintainer (inventory, compatibility and data-path acceptance); Composition and Translation (runtime selection and execution); Knowledge (cache and memory); Recovery (checkpoints and continuation); Formats (HTML/EPUB); Review (review exchange).
Issue: [#286 — REF-18](https://github.com/MichaelAlexandreDev/ayvu/issues/286).

## Acceptance record

Accepted on 2026-11-12 by the repository owner, through explicit approval in the development session
(translated quotation: "I accept all the rest", covering every recommendation of the REF-18 questions).
The answers to the [questions](#questions-for-the-owner) are the recommendations as written:

1. The inventory, the ten gaps G1–G10 and the shared extractions R1–R3 are accepted. The runtime stays
   until every gap has a recorded implementation or intentional-change disposition.
2. Old data takes path B: retranslate, keeping the verified v1 originals as immutable history. An
   archive importer (A) is optional, with zero promoted exact entries and no migrated completion credit.
3. Essential old offline/cache-only jobs finish with a frozen pre-retirement release first. Old files
   are kept until the owner validates the replacement books and authorizes deletion in another task.
4. With no model label after the switch, setup asks for a genuine owner-declared label and translation
   fails clearly before any provider call. There is no v1 fallback and no invented model identity.
5. Rich preview and dry run keep today's absence of project and job creation, through a transient
   route whose cache effects are specified before implementation.
6. Parallel workers and the high profile survive the switch. The effective worker count never changes
   silently.
7. Rich failure behavior and governed memory replace the old behavior: no partial EPUB on provider
   failure, fuzzy suggestions by default, and an explicit conservative policy for approved reuse.
8. JSON-v1 cache administration and CSV-v1 packages keep bounded inspection and export only during the
   transition. Positional CSV-v1 edits are never applied, and legacy namespace data is never relabelled
   as semantic v2.

The implementation follows the [ordered plan](#ordered-implementation-after-approval) from step 1,
one pull request at a time.

## Proposal and acceptance boundary

Retire the v1 runtime as decided in [D7](package-structure.md#d7-the-v1-runtime-in-quarantine).
This first delivery inventories the observable contract and proposes the old-data path. It changes
no runtime, default, cache, checkpoint, schema, fingerprint or user command. **Nothing is removed.**
The owner approved both this inventory and the data path on 2026-11-12; see the acceptance record above.

Evidence is the local REF-19 base `9e35382`, inspected without network access. No owner's cache,
checkpoint or book was opened; their existence is owner-supplied information, not a measured count.
Here, “v1” means the quarantined runtime, not every contract with a version number of 1. “Rich”
means the `DocumentTranslator` execution selected by a configured LibreTranslate model label.
Paths and tests reflect subsequent mechanical extractions; proposed commands/messages below are specifications only.

## Inventory and coverage

There are **ten open compatibility decisions/gaps, G1–G10**. Several are intentional differences
requiring acceptance rather than missing algorithms. Shared helpers still in v1 are listed separately
as R1–R3; already covered workflows are not counted again. A component test proves only its stated
scope, not end-to-end CLI parity. The current rich path still depends on v1 helpers.

The principal wiring is [composition/cli.py](../../../src/ayvu/composition/cli.py) and
[composition/builders/translate_project.py](../../../src/ayvu/composition/builders/translate_project.py).
`_translate_in_project` uses `translate_in_project` to select `LegacyTranslationRuntime` or
`RichTranslationRuntime`; `_translate_without_project` uses `translate_without_project` →
`v1.epub.translate_epub`. The latter handles preview, dry run and project-free old resumptions.

| Surface and observable v1 contract | Implementation and composition caller | Rich coverage, proof and exact remaining work |
| --- | --- | --- |
| **G1: `translate` without a model label**, including guided and batch translation | `LegacyTranslationRuntime` → [v1/translation.py](../../../src/ayvu/v1/translation.py) `LegacyUnitExecutor` / `LegacyExecutorPool`, through the project builder. `selected_model` returns `None` when the translator key is absent. | `RichTranslationRuntime` runs only with `translator_models.libretranslate` in `config.json`; its producer is `declared:<label>`, with command execution revision 2. [Selection test](../../../tests/composition/test_rich_runtime_selection.py) `test_production_composition_constructs_only_the_selected_runtime` proves both branches. [CLI execution](../../../tests/interfaces/cli/test_cli_rich_runtime_execution.py) proves actual stores, review and cache. Missing: the no-label default/setup policy; never silently manufacture a model label or reuse v1 entries. |
| **G2: root `--preview EPUB` and guided preview** produce a bounded translated EPUB, default first 12 documents, configured Preview folder, overwrite confirmation and validation, without project/job creation | [CLI preview](../../../src/ayvu/interfaces/cli/preview.py) → `composition.cli._translate_without_project` selects [translate_transient.py](../../../src/ayvu/composition/builders/translate_transient.py) only with a declared model. Without a label, the v1 path and its preparation timing remain unchanged. | `TransientTranslation` and `TransientUnits` share rich linguistic stages with G3. Preview uses authorized discovery, a real `declared:<label>` plan and the execution rate limiter; exact authorized v2 reads precede a private raw in-memory overlay, before glossary application. [Local publication](../../../src/ayvu/composition/builders/transient_publication.py) stages, validates and replaces the EPUB without project/job/lease/checkpoint/provenance/raw-results, Markdown or review packages. Formats supplies the same v1 enumeration and limit-before-selection; metadata consumes no document slot. [CLI tests](../../../tests/interfaces/cli/test_cli_rich_preview.py) and [publication tests](../../../tests/translation/test_transient_preview_publication.py) prove root/guided folders, 13-document enumeration, limits, overlay, SQL/tree snapshots, cancellation, overwrite and forbidden durable constructors. **Intentional differences pending owner acceptance:** no persistent cache warming; persistent hits only with an existing matching project; provider failure never publishes a partial EPUB. Workers (G8), memory (G9) and the default switch remain separate. |
| **G3: `--dry-run`**, also with workers, reads/counts/cache-lookups without provider translation or EPUB/project/job publication | Same project-free path into `v1.epub.translate_epub`; [v1/html_translate.py](../../../src/ayvu/v1/html_translate.py) → `LegacyTextPipelineFacade` in [pipeline.py](../../../src/ayvu/translation/pipeline.py). Existing preflight may create/check the v1 cache; this is not a promise of zero filesystem writes. | Labeled dry runs now use `TransientTranslation` / `TransientUnits` through [translate_transient.py](../../../src/ayvu/composition/builders/translate_transient.py), sharing rich linguistic stages, protection, per-unit canonical resolution and report conversion. `ReadOnlyExactCache(v2_cache_path(--cache))` reads only a unique existing matching project authorized via read-only catalog/manifest paths. No project/job/lease/checkpoint/provenance, provider/session/discovery, cache write/probe/sidecar or EPUB is created. Miss/ambiguous/unsupported count as work; corrupt/overflow fail; live rollback writers are busy, abandoned journals require recovery. Typed cache-only wins and preserves readable originals through existing missing-text renderers. Full-cache checks `missing > 0` only here. Configured workers/high remain visible with explicit sequential local analysis; Markdown offer and automatic batch reports remain. [CLI tests](../../../tests/interfaces/cli/test_cli_rich_dry_run.py) and transient Translation tests cover real stores, SQL/effect snapshots and forbidden network/durable constructors. **Intentional differences pending owner acceptance:** no cache warming or network; persistent hits require an existing matching project; full-cache uses missing count, so complete coverage exits 0 without EPUB. Unlabeled dry runs remain unchanged on v1. |
| **G4: `resume [EPUB] --target ...`, root/guided recovery offers**, invalid-file warnings, disambiguation and retry after interruption | [recovery/checkpoint.py](../../../src/ayvu/recovery/checkpoint.py) state/store and [recovery/checkpoint_scan.py](../../../src/ayvu/recovery/checkpoint_scan.py), called by `composition.cli.TranslationCheckpoint`, the project builder and `recovery.store`. [CLI resume](../../../src/ayvu/interfaces/cli/commands/resume.py) and recovery presentation invoke those roots. | Rich continuation exists: [CLI rich execution](../../../tests/interfaces/cli/test_cli_rich_runtime_execution.py) `test_rich_provider_failure_first_middle_last_is_resumable_without_partial_epub`; [raw resume tests](../../../tests/translation/test_raw_results_resume.py) `test_resume_replays_finished_documents_even_without_the_v2_cache`. Cross-runtime resume is deliberately refused before requests (`test_resume_across_runtimes_is_refused_and_names_both_before_any_request`). No conversion of v1 progress into proven rich results exists. Even rich checkpoints still use the class/file in `v1/`, with `version=1`, `execution_revision=2`; removing that module would break rich recovery too. |
| **G5: v1 cache**: exact text/language hits, writes, fuzzy candidate lookup, JSON-v1 administration and old-data accessibility | [v1/cache.py](../../../src/ayvu/v1/cache.py) `TranslationCache`, `CacheKey`; CLI translation/cache-check roots. [Cache builder](../../../src/ayvu/composition/builders/cache_admin.py) uses `SQLiteCacheStore` plus [knowledge/cache/legacy_admin_store.py](../../../src/ayvu/knowledge/cache/legacy_admin_store.py) `SQLiteLegacyStreamingStore` and [knowledge/cache/legacy_stream.py](../../../src/ayvu/knowledge/cache/legacy_stream.py); [knowledge/cache/legacy_format.py](../../../src/ayvu/knowledge/cache/legacy_format.py) validates bounded JSON-v1 payloads. | [repository.py](../../../src/ayvu/knowledge/cache/repository.py) `SQLiteExactCache` is a different contract: semantic identity and authorized scope. Production appends `.v2.sqlite` to the whole `--cache` filename and leaves v1 untouched. CLI rich execution `test_rich_translate_real_stores_cache_and_review_with_instrumented_transport` proves non-reuse and byte preservation. Missing: approved disposition of legacy namespace/JSON readers and defaults, not another exact-cache implementation. Details below. |
| **G6: legacy review CSV** and `apply-review` inspection/refusal | `v1.epub.translate_epub(review_segments=...)` collects positional `ReviewSegment` values; `v1.html_translate` also emits them. [csv_export.py](../../../src/ayvu/review/csv_export.py) retains the legacy writer. `v1.epub.apply_reviewed_epub` refuses `LegacyReviewInspection` with `legacy_reexport_required`; it does not apply CSV v1. | Current production `translate --review-output` already exports a **v2** package through [run.py](../../../src/ayvu/interfaces/cli/run.py), `open_review_use_cases` and [review builder](../../../src/ayvu/composition/builders/review.py), for project translations in either runtime. [CLI export test](../../../tests/interfaces/cli/test_cli_translate_review_export.py) `test_translate_command_delegates_v2_review_export_from_exact_project_result`, [CLI review tests](../../../tests/interfaces/cli/test_cli_review.py) `test_apply_review_command_rejects_v1_before_opening_epub_or_reconstructing`, and [legacy exchange tests](../../../tests/review/test_review_exchange_legacy.py) prove the boundary. Retain bounded inspection and re-export guidance; do not restore positional application. Decide whether the legacy writer has any remaining supported consumer; no new CSV identity migration is implied. |
| `extract EPUB --output/-o DIR --overwrite` and `inspect EPUB` | Already independent of `v1/`: [CLI content](../../../src/ayvu/interfaces/cli/commands/content.py) → `composition.cli.open_content_use_cases` → [content builder](../../../src/ayvu/composition/builders/content.py) → [formats/use_cases.py](../../../src/ayvu/formats/use_cases.py), `FormatService`, EPUB adapter, [Markdown writer](../../../src/ayvu/formats/markdown/writer.py). No v1 caller remains here. | Covered, not a retirement gap. [CLI content tests](../../../tests/interfaces/cli/test_cli_content.py) `test_extract_command_preserves_filenames_text_order_and_original`, `test_extract_command_keeps_the_nonempty_directory_conflict_message` and `test_inspect_command_preserves_the_six_current_fields`; [format extraction](../../../tests/formats/test_format_service_extraction.py) covers the application path. Preserve numbered Markdown filenames, visible-text order, directory conflict/partial-publication errors and untouched input. |
| **HTML v1**, including block/inline text, ignored script/style/code/pre, whitespace, comments, attributes, nested/void tags, literal markers, optional image alt, protected terms and ordered chunks | `v1.html_translate.translate_html` / `translate_text` / `apply_reviewed_html`, called by `v1.epub` and `v1.translation.LegacyUnitExecutor`; composition reaches them through the project builder. Structural extraction/reconstruction already delegates to Formats. | [html_structure.py](../../../src/ayvu/formats/epub/html_structure.py), `DocumentTranslator`, shared pipeline and [protection.py](../../../src/ayvu/content/protection.py) cover the responsibilities. [Rich protection test](../../../tests/translation/test_translation_document_protection.py) `test_an_html_document_survives_the_rich_path_and_reconstruction`, [HTML reconstruction tests](../../../tests/formats/epub/test_html_structure_reconstruction.py) `test_translated_text_never_becomes_markup`, and [extraction tests](../../../tests/formats/epub/test_html_structure_extraction.py) prove structure, tokens, whitespace and ignored text. Compare the remaining v1 characterization corpus before deletion; these tests do not claim all legacy malformed-input recovery is byte-equivalent. |
| **EPUB v1 reconstruction**, selected chapters, title/navigation metadata, alt text, unchanged members/assets, mimetype, validation and original preservation | `v1.epub.translate_epub` and `_copy_epub_with_replacements` are the project-free reconstruction; ordinary v1 project execution already uses `TranslationEngine` and the format adapter via `LegacyUnitExecutor`. Archive I/O is in [formats/epub/io.py](../../../src/ayvu/formats/epub/io.py), not v1. | [EPUB adapter](../../../src/ayvu/formats/epub/adapter.py) (`_EpubPorts.reconstruct` / `_materialize`), [archive.py](../../../src/ayvu/formats/epub/archive.py) and [engine.py](../../../src/ayvu/translation/engine.py) cover rich production. [Composition execution tests](../../../tests/composition/test_rich_runtime_execution.py) `test_rich_engine_reconstructs_actual_epub_preserving_structure` runs metadata/alt combinations and a cache repeat; EPUB reconstruction `test_only_translated_members_change_and_every_other_keeps_its_bytes` and `test_the_adapter_and_the_current_flow_write_the_same_package` provide additional parity. Remaining preview/dry-run integration is G2/G3; publication differences are G10. |
| **Preflight `cache_check`** must detect an unwritable cache before translation | [v1/cache_check.py](../../../src/ayvu/v1/cache_check.py) `verify_cache_writable` opens v1 and calls its rollback-based write probe. `composition.cli.run_translation_preflight` injects it into [preflight.py](../../../src/ayvu/translation/preflight.py); callers include preview and project-free runs. | Rich `preflight` constructs `SQLiteExactCache` before provider preparation; `prepare_translation(project_backed=True, label present)` supplies a no-op to the later legacy check. [Preparation tests](../../../tests/composition/test_rich_runtime_preparation.py) `test_prepare_cache_is_complete_before_provider_preparation_and_preserves_v1` and `test_cache_is_refused_before_preparation_and_errors_are_sanitized` prove it. Replace the default callback only after G2/G3/G7 are wired, preserving errors and ordering; do not just delete the injection parameter. |
| Markdown/terminal reports, counters, progress, errors, glossary usage, validation warnings, report directory and non-overwriting names | [CLI report](../../../src/ayvu/interfaces/cli/report.py) renders `translation.TranslationReport`. Both runtimes use `formats.epub.reader.read_package` and `translation.to_report` in the project builder. Single runs offer a report; batches auto-save reports. | Already visible in rich CLI: `test_each_batch_book_uses_rich_and_keeps_markdown_reports` in rich option tests; [report tests](../../../tests/interfaces/cli/test_cli_report.py) cover configured/default destinations, memory/context rows and declining the offer. R1 below is done: the shared report conversion/type moved without changing output. Missing-text output is G7; provider-failure output is G10. |
| **Glossary after translation and after raw-cache reuse**, never bake downstream substitutions into raw cache | `v1.html_translate.translate_text` → shared `LinguisticStages`: exact lookup, optional fuzzy, provider/protection/chunks, raw cache write, then `_finish` applies glossary and restores outer whitespace. `LegacyTranslationRuntime` supplies the glossary. | Covered: rich resources pass `apply_glossary_with_usage` to `DocumentTranslator`. [Rich cache identity test](../../../tests/translation/test_translation_cache_v2_identity.py) `test_rich_cache_hit_reapplies_downstream_glossary_after_protected_restoration`; [v1 characterization](../../../tests/v1/test_translate_text_characterization.py) `test_raw_translation_is_cached_before_the_glossary_changes_it` and `test_glossary_applies_to_a_cache_hit_without_rewriting_the_cache`. Preserve this invariant during every step. Governed memory's already-final approved values with matching pins intentionally skip another glossary pass; they are not raw v1 cache hits. |
| **G7: `--cache-only`, `--require-full-cache`, `--missing-output`**, no translator calls, use hits, leave source for misses, optional partial EPUB, block EPUB when full coverage is required; `.txt` missing originals | v1 cache/HTML execution and project engine, with `report.missing_texts`; [run.py](../../../src/ayvu/interfaces/cli/run.py) and CLI report save explicit or automatic `<book>-<target>-missing.txt`. `require-full-cache` and `missing-output` require cache-only; explicit missing-output is rejected for multiple books. | Labeled durable `--cache-only` now selects `ReadOnlyExactCache` before online preparation and runs [CacheOnlyDocumentExecutor](../../../src/ayvu/translation/cache_only_execution.py) with real project/job/lease authority. Each unit resolves canonical evidence locally; misses/ambiguous/unsupported preserve readable originals, while corrupt/overflow/storage refusal fail before reconstruction. Full-cache gaps end `PARTIALLY_COMPLETED/cache_incomplete`, publish TXT/report only and exit 1; partial allowed or complete valid output exits 0. Existing TXT renderer and batch auxiliary publisher are reused. No TM/fuzzy/replay/provider/cache write or new raw-results, even on complete hits; exact reuse lineage references validated entries. Internal pins require a durable resolver, with no public flag or compatible fallback. Durable offline workers are implemented as described in G8; online parallel workers remain refused. **Intentional differences pending owner acceptance:** project-scoped sibling v2 only, no v1 promotion, zero cache preparation/repair, conservative storage refusals, ambiguity as missing, corruption as fatal, and exact-only reuse without raw production. See [CLI coverage](../../../tests/interfaces/cli/test_cli_rich_cache_only.py) and [missing-output tests](../../../tests/interfaces/cli/test_cli_rich_missing_output.py). |
| **G8: performance profiles and `--workers N`**: per-worker translator/cache, stable book order, shared rate limiter and restrictions on memory | [kernel values](../../../src/ayvu/kernel/values.py) profiles; [translate_options.py](../../../src/ayvu/interfaces/cli/translate_options.py) resolves overrides; `LegacyExecutorPool`, engine executor pool, and project-free `v1.epub` threads. | **Offline implemented, online pending batch 5.** Labeled durable cache-only accepts the effective N, including high=4 and explicit overrides. [DocumentScheduler](../../../src/ayvu/translation/document_scheduler.py) admits a bounded window including completed but unconsumed documents; [DocumentWork](../../../src/ayvu/translation/document_work.py) returns complete document results and local warnings. Only the coordinator emits global sequences, calls observers and publishes the EPUB; cache-only still writes no compatibility checkpoint. Worker cache/provenance connections are per operation. The database header is read in a child process so no descriptor close releases a sibling reader's SQLite lock; a healthy verdict is remembered per file state (inode, size, timestamps). Cancellation/failure stops admission, cancels queued work and joins started tasks under the single renewable lease; definitive lease loss is observable and prevents publication. [Scheduler](../../../tests/translation/test_document_scheduler.py), [cancellation](../../../tests/translation/test_document_worker_cancellation.py), [recovery](../../../tests/translation/test_document_worker_recovery.py) and [CLI profiles](../../../tests/interfaces/cli/test_cli_rich_cache_only_workers.py) cover these contracts. Online keeps its original workers!=1 refusal until batch 5 adds provider ownership, a shared limiter and duplicate-key arbitration. Governed memory plus multiple workers remains refused before stores/network, with no clamp. Unlabeled v1, dry run and preview are unchanged. **Owner acceptance pending: design questions 5 (governed-memory restriction) and 6 (split G8 delivery and cooperative drain).** |
| **G9: `--translation-memory`, `--tm-apply-threshold`, `--tm-suggest-threshold`** over the v1 cache, including cache-only fuzzy reuse | `LegacyTranslationRuntime.resources` constructs [TranslationMemory](../../../src/ayvu/knowledge/memory/fuzzy.py) over `TranslationCache.fuzzy_candidates`; default thresholds 0.95 apply / 0.80 suggest, enabled only on request, incompatible with multiple workers. No concrete v1 import is needed by that memory module: it consumes a protocol. | Rich uses governed approved memory through [memory_retrieval.py](../../../src/ayvu/translation/stages/memory_retrieval.py); fuzzy is suggestion-only unless conservative auto-apply is explicitly selected at >=0.98. `--tm-legacy-cache-compatibility` reads eligible **v2 machine** candidates, never unstaged v1 rows (`repository.legacy_candidates`). [Retrieval pipeline tests](../../../tests/translation/stages/test_memory_retrieval_pipeline.py) and [v1 memory tests](../../../tests/v1/test_html_translate_cache_memory.py) cover distinct policies. Gap: approve the loss of automatic old-cache fuzzy reuse; do not advertise the rich compatibility flag as a v1 importer. |
| **G10: failures and partial publication**, `--fail-fast`, report/exit behavior and continuation | `v1.epub` and the legacy unit executor can retain source for failed units, record errors and publish a partial translation when fail-fast is off; fail-fast stops publication. The [v1 parity tests](../../../tests/v1/test_translation_engine_parity.py) compare the old and engine paths; [publication tests](../../../tests/v1/test_translation_engine_publication.py) characterize errors, invalid characters and refusal cases. | Rich provider failure always stops the book without publishing a partial EPUB; successful segments remain cached and completed documents may be replayed. CLI rich execution `test_rich_provider_failure_first_middle_last_is_resumable_without_partial_epub` proves this intentional change. Gap: explicit acceptance of stricter publication and corresponding error/report differences; do not weaken rich behavior merely to make a snapshot identical. Cache-only partial output remains a separate G7 contract. |

### Configuration, flags and output details that must survive review

The complete translation option surface is in [commands/translate.py](../../../src/ayvu/interfaces/cli/commands/translate.py).
The table above identifies the v1-specific branches. The following grouping prevents shared options
from being mistaken for v1-only features or silently dropped with their old callers:

- Input/output and selection: `--output/-o`, `--output-dir`, `--overwrite`, `--source`, `--target`,
  `--chapters`, multiple EPUBs and `--continue-on-error` already use shared output plans, chapter
  selection, project execution and batch coordination. Rich batch proof is the named rich option test;
  [workflow batch tests](../../../tests/interfaces/cli/test_workflow_batch_preview.py) and
  [workflow translation tests](../../../tests/interfaces/cli/test_workflow_translation_resume.py) freeze
  collision, overwrite and original-preservation behavior. No directory-input expansion is promised
  just because an old comparison script attempts it.
- `--translate-metadata` translates title/navigation text, not authors/identifiers; image alt is opt-in
  with `--translate-alt-text`, not OCR. The rich structure test above covers both switches. Preserve
  selection order, navigation handling, unchanged assets, links, protected terms and source bytes.
- Provider controls `--translator libretranslate`, `--url`, `--timeout`, `--retries`,
  `--requests-per-second`, `--retry-backoff`, `--retry-backoff-max`, `--chunk-limit` and invocation-only
  `--network-policy none|loopback` are shared through provider/preflight composition. Credentials in
  URLs and unsupported network routes remain refused. [Rich policy tests](../../../tests/interfaces/cli/test_cli_rich_runtime_policy.py)
  and [preflight route tests](../../../tests/translation/test_preflight_provider_routes.py) cover those
  gates. Cache-only must avoid discovery as well as translation; an ordinary warm rich cache run still
  performs language discovery, as the rich CLI execution test proves.
- `--profile` resolves saved target/glossary defaults; `--glossary` and config profile/glossary paths
  remain effective, and legacy `style` stays inert. `translator_models.libretranslate` is presently
  the runtime selector; changing a server's model requires changing its declared label. Configured
  library `books_dir`/folders, `preview_dir` and `reports_dir` remain presentation/output preferences,
  not cache identity. Tests: [target profiles](../../../tests/interfaces/cli/test_cli_translate_target_profiles.py),
  [model labels](../../../tests/preferences/test_rich_runtime_model_label.py), preview/report tests above.
- Performance settings `(workers, requests/s, retries, initial/max backoff, chunk limit)` are:
  `low=(1,1,4,1/12,1500)`, `standard=(1,unlimited,2,0.5/8,3000)`,
  `high=(4,unlimited,2,0.5/8,4000)`, `custom` starts from standard. Explicit execution flags override
  the profile. They are not the same as translation `--profile`; old checkpoints store effective
  values, not the performance profile name. Do not silently clamp high/workers during retirement.
- Existing report fields include chapters, translated/cache/memory/suggestion/missing/alt counts,
  errors, validation and glossary required/forbidden/usage information. Rich keeps the normal reports
  through the shared Translation report values/conversion (R1 done). Progress callbacks and terminal wording
  are observable alongside files; G10 explicitly identifies the failure-path changes.
- Legacy CSV columns are `segment_id`, `source_epub`, `output_epub`, `chapter_index`, `chapter_name`,
  `document_name`, `document_path`, `segment_kind`, `source_language`, `target_language`, `from_cache`,
  `original`, `translated`. Positional IDs such as `c0001-s0001` and `metadata-title` cannot establish
  v2 unit identity. `apply-review ORIGINAL CSV --validate-only` inspects/refuses legacy input rather
  than authorizing application; `--output/-o` and `--overwrite` apply only to valid v2 reconstruction.
  V2 export is identifiable by the `AYVU-REVIEW-PACKAGE,2,` preamble, not by a `.csv` extension.

### Legacy cache administration is its own compatibility surface

[CLI cache commands](../../../src/ayvu/interfaces/cli/commands/cache.py) offer `inspect`, `clean`,
`export` and `import`. Without a namespace they operate the legacy cache; selecting v2 requires
explicit namespace/scope/project authority. The default `--cache` is `.cache/traducoes.sqlite`;
rich production therefore uses `.cache/traducoes.sqlite.v2.sqlite`. Filters are `--source`, `--target`, `--before`; clean
requires a filter or `--all`, previews with `--dry-run`, and requires `--yes` for mutation. Export
accepts a positional output file and `--overwrite`; import accepts a file and `--replace`. Outputs include counts
by language pair and creation range, selection counts, and inserted/replaced/kept import counts.
Absent read-only inspect/export must not create the cache. Preserve refusal of exporting over the
database/sidecars, bounded parsing, duplicate/hash validation, transaction rollback, cancellation,
lock waiting, output permissions and atomic file publication.

Evidence: [legacy CLI tests](../../../tests/interfaces/cli/test_cache_admin_legacy_commands.py),
[CLI cache tests](../../../tests/interfaces/cli/test_cli_cache.py),
[streaming store tests](../../../tests/knowledge/cache/test_cache_admin_legacy_stream.py),
[stream parser tests](../../../tests/knowledge/cache/test_cache_legacy_stream.py) and
[administration gate tests](../../../tests/knowledge/cache/test_cache_admin_v2_gate.py).
V2 already implements scoped inspect/clean/export/import in
[admin_v2_store.py](../../../src/ayvu/knowledge/cache/admin_v2_store.py) and its siblings; proof:
[v2 CLI tests](../../../tests/interfaces/cli/test_cache_admin_cli_v2.py) and
[round-trip tests](../../../tests/knowledge/cache/test_cache_admin_v2_roundtrip.py).
That does not make JSON-v1 a semantic-v2 import. `CacheScopeMapping` explicitly refuses cross-kind
mapping (`legacy_scope_isolated`), tested by [v2 admin contracts](../../../tests/knowledge/cache/test_cache_admin_v2_contracts.py)
`test_map_rejects_duplicate_sources_all_and_legacy_promotion`. Legacy operations on v2/partial databases require v2 administration
(`v2-admin-required`), preventing accidental projection or destructive “upgrade”. This is part of G5.

## Imports from lower layers and the remaining cycle

R3 was completed on 2026-11-12 as the Knowledge extraction in step 1. Knowledge now owns
`legacy_schema.py`, `legacy_format.py`, `legacy_stream.py` and `legacy_admin_store.py` under
`knowledge/cache/`. The `legacy_` prefix distinguishes these preserved contracts from semantic v2
administration. The SQL bytes, unique key, JSON-v1 validation, messages and namespace are unchanged.
Bounded JSON-v1 inspection and export remain available during the transition, as accepted in answer 8;
existing import and cleanup behavior is also unchanged by this mechanical extraction.

R2 was completed on 2026-11-12 as the Recovery extraction in step 1: the shared checkpoint and its
bounded scanner moved intact to `recovery/checkpoint.py` and `recovery/checkpoint_scan.py`, so the
former `recovery.store → v1.resume` and `recovery.store → v1.resume_scan` imports are now internal
Recovery imports. With R2 and R3 done, **no REF-18 import-linter exception remains** in
[pyproject.toml](../../../pyproject.toml). The G4 legacy-data disposition still applies.

The three former Knowledge exceptions have been removed: administration imports the local
`legacy_format` codec; `admin_store` and `schema` import the single local `legacy_schema.SCHEMA`
definition, with `schema` retaining its `LEGACY_SCHEMA` binding. `v1.cache` consumes `SCHEMA` through
Knowledge's public value API; `TranslationCache` and `v1/cache_check.py` remain in v1.
New v2 files still contain `translations` and the ledger's `legacy_namespace='legacy-v1'`;
changing them would require a separate versioned D9 migration.

`scripts/arch_metrics.py` immediately before R3 reported one cycle, `knowledge ↔ v1`; after the
extraction it reports none. Composition now constructs `SQLiteLegacyStreamingStore` from Knowledge.
The streaming reader and store consume local administration ports, limits, interchange and schema,
so Knowledge has no remaining import of v1. R1 and R2 remain separate work.

Other composition dependencies requiring extraction before deleting the directory:

- **R1 done:** Both runtimes, including rich `facts()`/`report()`, now use
  `formats.epub.reader.read_package`, `ayvu.formats.BookPackage`/`BookFacts` and Translation's public `TranslationReport`,
  `HtmlTranslationStats`, `TextTranslationResult`, `to_html_stats` and `to_report`, implemented in
  [translation/report.py](../../../src/ayvu/translation/report.py). Definitions, output and persisted
  identities are unchanged; consumers import their new owner without a re-export shim.
  [formats/epub/reader.py](../../../src/ayvu/formats/epub/reader.py) is a public I/O adapter, outside
  the EPUB package exports. `BookFacts` and `BookPackage` moved verbatim from
  `translation.engine` and `v1.translation` to the I/O-free
  [formats/book_facts.py](../../../src/ayvu/formats/book_facts.py), so Translation imports them
  downwards and no layer exception is added; Translation's package API loads no reader or archive
  adapter. The older ownership checker keeps, for `translation.report`, the exact exemption
  `v1.epub` already had for `GlossaryUsage`, whose module still combines values with I/O.
- **R2 done (2026-11-12):** `TranslationCheckpoint` creates/saves through Recovery-owned
  `ResumeStateStore`, including rich `execution_revision=2`. The intact implementation and scanner
  moved to `recovery/checkpoint.py` and `recovery/checkpoint_scan.py`, without a shim or byte/default change.
- **R3 (completed):** Cache composition constructs Knowledge's `SQLiteLegacyStreamingStore`; its
  bounded codec/storage and shared schema no longer depend on v1. The v1 writable probe stays in
  `v1/cache_check.py` until the runtime is retired.

## Old data: alternatives and recommendation

### Facts that bound any conversion

The v1 SQLite table contains `id`, `source_lang`, `target_lang`, `original_text_hash`,
`original_text`, `translated_text`, `created_at`; its unique key is the language pair plus SHA-256
of the original UTF-8 text. Text can be a tokenized block template, not just visible prose. The
runtime stores restored protected terms before downstream glossary processing, but a row carries
no trustworthy stage declaration and could have been imported. It records no producer/model,
route hops/capabilities/configuration snapshot, semantic pipeline/segmentation/protection/
normalization revisions, chunk limit, prompt/context/terminology identity, authorized scope,
document revision/unit binding or proven approval. A matching string, URL or present-day label
does not recover any of those facts.

V2 [identity.py](../../../src/ayvu/knowledge/cache/identity.py),
[entries.py](../../../src/ayvu/knowledge/cache/entries.py) and scope policy require those semantic
dimensions, value kind and provenance, separately from the source digest. Unknown producer data
is ineligible for exact reuse; [document cache tests](../../../tests/translation/test_translation_document_cache_memory.py)
`test_the_legacy_v1_ports_do_not_fit_the_generic_path` and
`test_the_generic_path_cannot_reach_the_legacy_cache` freeze the boundary. **For a standalone v1
cache, zero rows can honestly become eligible v2 exact hits from that cache alone.** Independent
durable evidence could justify an individually verified reconstruction in a separate proposal;
this document authorizes neither guessed identity nor blanket promotion/approval.

V1 `*.ayvu-state.json` is an invocation/progress record: paths, languages, translator URL/name,
glossary/profile, execution controls, selection/metadata/alt/memory settings, timestamps and
completed/failed/current chapter names/counts. Old missing optional fields receive defaults;
missing execution revision means 1. It holds neither raw translations nor rich per-unit identity.
Chapter completion lists are not proof of reusable rich results. Old resume re-runs extraction and
uses the v1 cache, rather than restoring a partially built EPUB from those lists. A matching
project may supply a source hash and old job association, but not a missing v2 producer/stage identity.
[Recovery CLI tests](../../../tests/interfaces/cli/test_cli_recovery.py)
`test_an_old_file_runs_as_it_always_did_with_no_project_and_no_job` and
`test_an_old_file_stays_byte_for_byte_when_it_is_only_read_or_when_the_translator_is_down` capture this.

### A. One-time import into v2 storage, with legacy quarantine

For caches, an importer can preserve the six interchange fields exactly in `legacy-v1` within a
separate v2 container, plus an external manifest of source digest, schema, row/key counts and
validation/rejection counts. It must not populate semantic exact entries. The existing
[schema preparation](../../../src/ayvu/knowledge/cache/schema.py) already demonstrates this distinction:
`prepare_cache` preserves legacy rows, makes a verified `.v1-backup.sqlite`, and builds an empty
`cache_entries_v2`. Production rich composition deliberately refuses a v1 database placed at the
sibling filename; this lower-level migration is **not** the production import path today.
An approved import would operate on a copy/snapshot, not the owner's live source in place.

For states, convert only validated invocation settings into a **new translation request**, retaining
the original checkpoint as evidence and recording an import/source reference without claiming
`continuation_of` a rich job. Re-inspect the book and resolve genuine current model/configuration
and project identity; require a destination plan. Do not copy completed chapters into completed
rich units or change `execution_revision` in the old JSON. Report unreadable/invalid/unsupported
states individually; one bad file must not hide the others. An actual rich checkpoint with execution
revision 2 is preserved/moved by R2, not downgraded into this legacy conversion.

Benefits: archives pairs and invocation settings, aids audit/manual review, avoids retyping valid
options. Costs: importer, archive support and verification obligations; no reduction in provider
work without independently established identity. Lost for execution: legacy warm hits/fuzzy reuse,
chapter progress credit and unproved lineage, not the archived bytes. JSON export omits internal
SQLite row IDs, so retain the SQLite snapshot if IDs/physical history matter.

The user would see “legacy archive imported; N pairs retained, 0 eligible exact entries” plus
accepted/rejected counts, and “old progress archived; new translation required” for states. These
are proposed messages, not current UI. Undo by deselecting the newly created archive/request and
using the untouched originals with the frozen pre-retirement version; remove only newly owned
outputs after verification. Restoring a whole migrated database backup later would discard new
v2 writes, so never overwrite an active database as an automatic rollback.

### B. Translate again with the rich runtime — recommended

Keep verified offline copies of the old caches/checkpoints and use the original EPUB to start a new
rich job with a real declared model, current glossary/options and a new destination. Build the v2
cache from newly observed provider work; subsequent runs/recovery reuse only proven v2 entries
and raw artifacts. Offer an explicit bounded summary of old invocation settings if useful, without
calling it progress migration. The first new run has no v1 hits and may take a full translation's
time; wording/segmentation may change with provider or pipeline. Existing independently valid v2
entries remain reusable under their own identities.

Benefits: smaller retirement scope, honest identity/provenance, no speculative conversion or
long-lived executable v1 fallback. Costs: compute/time, unavailable old model risk, changed wording
and manual review of differences. An old cache-only book cannot be reproduced offline from v1
rows by this route; the owner must finish it with the frozen v1 release before removal or accept
the loss. Preserve old outputs and reviewed text for comparison; retranslation does not recreate
human review or approve a new translation.

The user sees a legacy-data notice before work, the new cache location, the need to translate again
and an explicit output destination; rich `resume` continues only the new job chain. No silent
deletion, overwriting of originals or claim of migrated progress. Undo by retaining the old output,
restoring the old configuration in an isolated pre-retirement checkout and selecting the old
cache/state copies. A rich job is never resumed as v1. Keep both configurations/data sets separate;
rollback after runtime removal uses the frozen release, not a hidden compatibility shim.

**Recommendation:** B for executable results, with immutable legacy snapshots and optional A only
as archival interchange/request assistance when the owner values it. Do not build an exact-cache
importer that cannot establish exact identity. Before removal, decide whether remaining v1 jobs
must finish on the frozen version and whether archival JSON-v1 administration remains supported.

### Integrity proof required for either accepted path

Use only synthetic fixtures for development, including tokenized/Unicode/whitespace text, imported
rows, conflicts, corrupt hashes, malformed states, missing sources and interrupted writes. A later
owner-authorized real-data procedure must first stop writers or take a consistent SQLite snapshot
including committed WAL data; hashing only the live main file is insufficient. Keep source/backup
digests and manifest, `PRAGMA integrity_check`, counts per language/key, every preserved field and
recomputed source hash. Compare the complete sorted logical row set, not just counts; maintain an
explicit rejected/conflict ledger, never silently repair, normalize, deduplicate or replace data.

Prove that imported legacy rows yield **no** v2 exact hits and no invented model/stage/approval;
source snapshots/checkpoints and existing outputs stay byte-identical. For states, round-trip all
present/absent defaults, reject cross-runtime continuation, and prove no fabricated completed units.
For retranslation, verify fresh lineage, cache isolation, original and untranslated member bytes,
HTML/token integrity, EPUB validation and a later resume with the provider unavailable where
proven results suffice. New translations need human quality comparison, not byte equality to v1.
Exercise cancellation, disk-full/permission failure, concurrent writers and a crash at each import
phase; verify reopen, idempotency and rollback without damaging existing v2 entries.
Existing examples are [schema migration tests](../../../tests/knowledge/cache/test_cache_schema_v2_migration.py),
[snapshot recovery tests](../../../tests/knowledge/cache/test_cache_schema_v2_snapshot_recovery.py),
v2 round-trip tests and raw-results resume tests above; no one-time retirement importer is tested
or implemented by this delivery.

## Ordered implementation after approval

The following are proposed REF-18 follow-up issue/PR scopes, **not created issue numbers**. Keep
one migration open at a time (D13); gap closure is retirement work, not unrelated feature work.
Do not mix file moves with a behavior change. Any accepted contract change gets its own behavioral
PR, updated decision/test expectation and explicit owner acceptance. D9 identities remain frozen
unless a separately approved versioned migration changes them.

| Order / proposed issue or PR | Kind under D13 | Exit proof |
| --- | --- | --- |
| 0. Accept this inventory, G1–G10 dispositions and cache/state path | Documentation only | Recorded owner answers to the questions; no production/data change. Recorded on 2026-11-12 in the acceptance record. |
| 1. Extract R1–R3 to Translation/Formats, Recovery and Knowledge, in sequential focused PRs (R1 done; this extraction does not change R2/R3) | Mechanical, use `git mv` where applicable, no shims | Same reports, checkpoint bytes/defaults and cache schema/namespace; imports updated in the same PR. Remove matching ignores and prove the Knowledge/v1 cycle disappears. Preserve the rich checkpoint format as well as old readers. |
| 2. Close G2/G3 preview/dry-run integration, G7 cache-only/missing outputs, G8 workers or approved sequential profile policy; one focused PR per behavior | Behavioral | CLI tests with and without configured labels before changing the default. Preview selection/validation, dry-run side effects, no DNS/session/provider in cache-only, ambiguous/corrupt identity, full-coverage no-output, missing originals and worker cancellation/order. G5/G6 readers must remain available pending data disposition. |
| 3. Settle G1 model setup, G9 memory and G10 failure/publication differences; lock the approved compatibility matrix | Behavioral | Explicit acceptance and tests for each intended difference; preserve glossary-after-raw-cache invariant. No fabricated default identity or implicit v1 fuzzy promotion. Verify existing shared flags, extraction, review, reports and HTML/EPUB corpus against both current branches. |
| 4. Switch production default/selection to rich, including guided/batch/project-free surfaces | Behavioral | Before switch: run comparisons against frozen main once without labels and once with real configured fixture labels; record expected branch differences, never claim cross-runtime byte parity. After switch: e2e CLI with a real loopback HTTP server, new default/setup gate, preview/dry run, cache warm repeat, cache-only and interruption/resume, review export/apply and report publication. Keep old-data handling explicit while pending final cleanup. |
| 5. Deliver the accepted old-data path: B notices/new requests, optionally A archive importer | Behavioral | Synthetic integrity/crash/rollback proof above, count/digest manifest and user-visible outcomes. Only then perform any separately authorized real-data operation. Old files remain retained; no bulk identity promotion. If B is chosen, this is a migration guide/notice, not an unnecessary importer. |
| 6. Remove unused `v1/` code and imports, legacy execution selection and obsolete tests after their useful characterization has moved to the new owners | Mechanical only after earlier PRs make deletion behavior-neutral | No production/test imports or lower-layer ignores pointing to v1; no v1 reachability. Keep supported archival/checkpoint contracts in their owning modules. Full e2e after deletion and rollback rehearsal using frozen release/copies. Any remaining behavior removal, including legacy admin/CSV writer, happens in a prior separate behavioral PR. |

For every code PR: full suite plainly and with `GITHUB_ACTIONS=true`, Docker stage, persisted
identity tests, link/package checks, import/public-API/size checks, `scripts/arch_metrics.py` and
`git diff --check`. Compare source changes with frozen main using the 71 CLI scenarios in
`ayvu-auto/ferramentas/compare158.py` and the 42 translation/resume scenarios in
`compare_resume159.py`, adapting **copies** to current CLI syntax, interpreter, isolated fixture
directories and label/no-label configuration. Do not execute the historical scripts unchanged:
they contain absolute paths and old invocations, and the resume script writes under ferramentas.

Those scripts inspect terminal output, provider requests, cache rows, EPUB members/output hashes,
state JSON, auxiliary files, file modes and staging leftovers; their scenarios include extract,
preview, glossary, profiles/workers, cache-only/missing output, review, overwrite, signal/kill,
old/invalid/several states, missing/changed/protected source and provider-down recovery. Normalized
timestamps/IDs and sorted text hashes can hide differences; retain exact identity/row/token tests
and targeted assertions alongside snapshots. Reading the scripts is inventory evidence, not a claim
that comparisons/e2e/Docker were run for this documentation-only delivery.

## Questions for the owner

1. **Is the inventory complete enough to accept, including all ten gaps and shared R1–R3?**
   Recommendation: accept only after checking any personally relied-on command/output against it;
   keep the runtime until every gap has a recorded implementation or intentional-change disposition.
2. **May we retranslate old data instead of treating v1 pairs/progress as exact v2 results?**
   Recommendation: B; retain verified immutable originals. Add A only for archive/request convenience,
   with zero promoted exact entries and no migrated completion credit.
3. **Must old offline/cache-only jobs finish before removal, and how long are old files retained?**
   Recommendation: finish essential jobs with a frozen pre-retirement release first; retain snapshots
   until the owner validates replacement books and explicitly authorizes deletion in another task.
4. **What happens when no model label is configured after the switch?**
   Recommendation: require a genuine owner-declared label in setup and fail clearly before provider
   calls if missing; no v1 fallback or invented model identity. Accept the change to unattended runs.
5. **Must rich preview/dry run preserve today's absence of project/job creation?**
   Recommendation: yes; specify a transient route and its cache effects before implementation.
6. **Must parallel workers/high profile survive, or is sequential-only operation acceptable?**
   Recommendation: preserve workers if relied on; otherwise explicitly accept a sequential high-profile
   redesign before the default switch. Never silently change the effective worker count.
7. **Accept rich failure behavior and governed memory instead of old partial EPUB/fuzzy auto-reuse?**
   Recommendation: yes: no partial EPUB on provider failure, fuzzy suggestions by default, explicit
   conservative policy for approved reuse; old unstaged cache rows remain excluded.
8. **Retain JSON-v1 cache administration and CSV-v1 inspection after runtime removal?**
   Recommendation: retain bounded archive inspection/export and CSV re-export guidance in Knowledge/
   Review for the transition; decide the need for legacy clean/import/writer separately. Never apply
   positional CSV-v1 edits or relabel legacy namespace data as semantic v2.
