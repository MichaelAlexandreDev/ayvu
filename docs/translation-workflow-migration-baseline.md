# Current Translation Workflow Migration Baseline

This baseline records the observable CLI contracts at `main` commit `047efd9`.
Any architectural migration must preserve these behaviors or deliberately revise
this document and its linked tests in a separately reviewed change.

## Invariants

- The input EPUB is never modified; translation and review create new EPUBs.
- Only reader-visible text is translated. HTML structure, ignored content,
  links, CSS, images, internal names, and unrelated ZIP members are preserved.
- `mimetype` remains the first, uncompressed EPUB member.
- Exact cache entries are scoped to the source/target language pair and receive
  glossary application after retrieval.
- Resume state remains compatible with cache-backed continuation.
- External translator behavior is reached only through the LibreTranslate HTTP
  boundary and remains testable with fakes.

## Characterization coverage

| Current workflow or contract | Observable baseline | Automated evidence |
| --- | --- | --- |
| Inspect input EPUB | Read title, author, language, documents, and item count without changing the source. | `tests/test_epub_io.py::test_inspect_epub_reads_minimal_generated_epub` |
| Output naming | A single translation written through `--output-dir` is named `<stem>-<target>.epub`. | `tests/test_workflow_characterization.py::test_translate_cli_preserves_source_archive_and_visible_text` |
| Original-file and archive preservation | Source bytes stay unchanged; ZIP member order is retained; `mimetype` is stored; non-replaced CSS, image, and binary members retain bytes and metadata. | `tests/test_workflow_characterization.py::test_translate_cli_preserves_source_archive_and_visible_text` |
| Visible-text selection | Visible blocks are translated, while comments and `script`, `style`, `code`, `pre`, `kbd`, `samp`, `svg`, and `math` remain untouched and are never sent to the translator. | `tests/test_workflow_characterization.py::test_translate_cli_preserves_source_archive_and_visible_text`; `tests/test_html_translate.py::test_translate_html_ignores_script_style_code_pre` |
| Preview | Only the configured initial document limit is translated for a preview. | `tests/test_epub_io.py::test_translate_epub_limits_documents_for_preview` |
| Selected chapters | Selected chapters change and unselected chapters are copied unchanged. | `tests/test_epub_io.py::test_translate_epub_translates_selected_chapter_by_index_without_changing_others` |
| Exact cache | A repeated workflow uses cached translations without another translation POST. | `tests/test_workflow_characterization.py::test_translate_cli_reuses_exact_cache_and_applies_glossary_after_cache` |
| Cache-only execution | Cached content can rebuild an EPUB without calling the translator; missing content is reported or blocks output when requested. | `tests/test_epub_io.py::test_translate_epub_cache_only_reuses_cache_without_calling_translator`; `tests/test_epub_io.py::test_translate_epub_cache_only_require_full_cache_blocks_output_when_missing` |
| Fuzzy translation memory | Similar plain-text segments can be applied or suggested conservatively; markup-bearing blocks are excluded. | `tests/test_html_translate.py::test_translate_html_reuses_memory_for_similar_plain_block`; `tests/test_html_translate.py::test_translate_html_memory_skips_blocks_with_inline_tags` |
| Glossary ordering | The glossary transforms both fresh and cached translations after cache lookup. | `tests/test_workflow_characterization.py::test_translate_cli_reuses_exact_cache_and_applies_glossary_after_cache`; `tests/test_html_translate.py::test_translate_text_applies_advanced_glossary_rules_to_cache_hits` |
| HTTP retries, rate limits, and workers | Retry/rate policy is shared across language discovery and translation; parallel workers use safe factories and preserve EPUB/review order. | `tests/test_translator.py::test_libretranslate_rate_limiter_is_shared_between_languages_and_translate`; `tests/test_epub_io.py::test_translate_epub_parallel_workers_preserves_order_and_review_segments` |
| Resume checkpoints | An interruption leaves a running state; `resume` reuses cached completed text, translates pending text, and marks the state complete. | `tests/test_workflow_characterization.py::test_translate_cli_interrupts_then_resume_reuses_checkpoint_and_cache` |
| Route resolution | A missing direct route uses the supported intermediate route and sends both HTTP translation legs. | `tests/test_workflow_characterization.py::test_translate_cli_resolves_intermediate_route_end_to_end` |
| Review export and import | CLI still exports positional v1 CSV for inspection; applying v1 now fails closed. The v2 library contract maps stable-unit edits to structural reconstruction inputs but is not connected to CLI publication. | `tests/test_workflow_characterization.py::test_legacy_review_export_is_inspectable_but_v1_cli_apply_is_fail_closed`; `tests/test_review_exchange.py::test_production_epub_extraction_review_mapping_and_staged_html_reconstruction` |
| Batch and reports | Batch processing writes one output and Markdown report per EPUB and can continue after a failed item. | `tests/test_cli.py::test_translate_command_batch_writes_outputs_and_markdown_reports`; `tests/test_cli.py::test_translate_command_batch_continues_after_failure_when_requested` |
| Final validation | Generated EPUBs are validated for visible empty chapters, broken internal links, and missing images before successful CLI completion. | `tests/test_workflow_characterization.py::test_translate_cli_preserves_source_archive_and_visible_text`; `tests/test_validation.py::test_validate_reports_broken_internal_link`; `tests/test_validation.py::test_validate_reports_missing_referenced_image` |

## Deliberate limits of this baseline

This is characterization, not a new application architecture. It does not add a
translator backend, a Desktop interface, a persistence schema, a new file format,
or a production refactor. Existing focused unit tests remain authoritative for
their narrow contracts; the new CLI workflows protect the cross-module seams
that a migration could otherwise preserve only superficially.
