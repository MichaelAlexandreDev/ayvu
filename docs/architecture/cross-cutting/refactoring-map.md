# Refactoring file map

This is the file map of the [package-structure decision](../decisions/package-structure.md) (Accepted), moved here by REF-20. It records where each flat module of the source tree at `7902aba` was planned to go; it is a migration map, not an index of the current files.


Every production file at `7902aba` appears once. "split" marks files over 800 lines, which REF-14 and
REF-15 break up by reason to change after they move.

| Current | Target | Note |
| --- | --- | --- |
| `__init__.py` | `__init__.py` |  |
| `domain.py` | `kernel/values.py` | split by owner when one appears |
| `migration_contracts.py` | `kernel/sqlite/contracts.py` |  |
| `sqlite_migrations.py` | `kernel/sqlite/migrations.py` |  |
| `network_policy.py` | `network/policy.py` |  |
| `network_http.py` | `network/http.py` |  |
| `preferences.py` | `preferences/model.py` | merged with `config_values.py` |
| `config_values.py` | `preferences/model.py` | merged with `preferences.py` |
| `preferences_service.py` | `preferences/service.py` |  |
| `preferences_store.py` | `preferences/store.py` |  |
| `config.py` | `preferences/config_facade.py` |  |
| `library_service.py` | `library/service.py` |  |
| `library_adapters.py` | `library/adapters.py` |  |
| `provenance.py` | `provenance/model.py` |  |
| `provenance_service.py` | `provenance/service.py` |  |
| `provenance_store.py` | `provenance/store.py` |  |
| `content.py` | `content/model.py` |  |
| `content_use_cases.py` | `formats/use_cases.py` | moved in REF-07 by the owner's decision of 2026-11-12; Formats owns the application orchestration, avoiding a cycle with content |
| `translation_protection.py` | `content/protection.py` | used by formats and translation |
| `job.py` | `jobs/model.py` | `is_slug`, `validate_job_identity` → `kernel/ids.py`; `validate_utc` → `kernel/clock.py` |
| `job_events.py` | `jobs/events.py` | `JobFailure`, `JobFailureFamily`, `JobWarning`, `normalize_failure` → `kernel/failures.py` |
| `job_execution.py` | `jobs/execution.py` | `CancellationToken`, `CancellationStatus`, `JobCancellationError` → `kernel/cancellation.py` |
| `job_service.py` | `jobs/service.py` |  |
| `job_store.py` | `jobs/store.py` |  |
| `artifact.py` | `artifacts/model.py` |  |
| `artifact_service.py` | `artifacts/service.py` |  |
| `artifact_store.py` | `artifacts/store.py` |  |
| `artifact_destination.py` | `artifacts/destination.py` | calls formats through a port: composition passes the output guard and the format publication in (REF-08) |
| `format_adapter.py` | `formats/contracts.py` |  |
| `format_service.py` | `formats/service.py` |  |
| `format_local.py` | `formats/local.py` |  |
| `epub_archive.py` | `formats/epub/archive.py` |  |
| `epub_format.py` | `formats/epub/adapter.py` |  |
| `epub_io.py` | `formats/epub/io.py` | split in REF-10: reading the EPUB stays here; the v1 translation of a whole EPUB, with its review CSV, went to `v1/epub.py`, since only v1 uses it |
| `html_structure.py` | `formats/epub/html_structure.py` |  |
| `validation.py` | `formats/epub/validation.py` |  |
| `markdown_writer.py` | `formats/markdown/writer.py` |  |
| `cache_identity.py` | `knowledge/cache/{identity_values,identity,scope,entries,cache_only,compatible_memory}.py` | split |
| `cache_schema.py` | `knowledge/cache/schema.py` |  |
| `cache_repository.py` | `knowledge/cache/repository.py` |  |
| `cache_interchange.py` | `knowledge/cache/interchange.py` |  |
| `cache_admin.py` | `knowledge/cache/admin.py` |  |
| `cache_admin_store.py` | `knowledge/cache/admin_store.py` |  |
| `cache_admin_v2_store.py` | `knowledge/cache/admin_v2_store.py`, `admin_v2_queries.py`, `admin_v2_files.py`, `admin_v2_export.py`, `admin_v2_records.py`, `admin_v2_import.py` | split by cache administration responsibility in REF-15 |
| `translation_memory.py` | `knowledge/memory/fuzzy.py` | in REF-09 it takes its candidates through the `FuzzyCandidates` protocol, so knowledge no longer imports the v1 cache |
| `approved_memory.py` | `knowledge/memory/approved/model.py` |  |
| `approved_memory_service.py` | `knowledge/memory/approved/service.py` |  |
| `approved_memory_store.py` | `knowledge/memory/approved/store.py` |  |
| `glossary.py` | `knowledge/glossary/rules.py` |  |
| `glossary_catalog.py` | `knowledge/glossary/catalog.py` |  |
| `glossary_catalog_store.py` | `knowledge/glossary/catalog_store.py` |  |
| `provider_capabilities.py` | `providers/capabilities.py` |  |
| `provider_discovery.py` | `providers/discovery.py` |  |
| `provider_service.py` | `providers/service.py` |  |
| `provider_network.py` | `providers/network.py` |  |
| `provider_libretranslate.py` | `providers/libretranslate/adapter.py` |  |
| `provider_session.py` | `providers/session.py` | added in REF-05, out of `provider_composition.py` |
| `project.py` | `projects/model.py` |  |
| `project_service.py` | `projects/service.py` |  |
| `project_store.py` | `projects/store.py` |  |
| `project_schema.py` | `projects/schema.py` |  |
| `project_profiles.py` | `projects/profiles/model.py` |  |
| `project_profile_service.py` | `projects/profiles/service.py` |  |
| `project_profile_store.py` | `projects/profiles/store.py` |  |
| `profile_glossary_store.py` | `projects/profiles/glossary_store.py` |  |
| `provider.py` | `providers/ports.py` | changed in REF-11: translation imports providers (D3), so the port it calls sits with providers; in `translation/` the adapters would import upward |
| `translation_plan.py` | `translation/plan.py` |  |
| `translation_pipeline.py` | `translation/pipeline.py` |  |
| `translation_engine.py` | `translation/engine.py` |  |
| `translation_document.py` | `translation/document.py`, `document_contracts.py`, `raw_results.py`, `document_replay.py`, `document_provenance.py`, `document_bindings.py` | split by document responsibility in REF-15 |
| `chunking.py` | `translation/chunking.py` |  |
| `preflight.py` | `translation/preflight.py` | in REF-11 the v1 cache check became `v1/cache_check.py`, passed in by composition as `cache_check` |
| `resume_reuse.py` | `translation/resume_reuse.py` | added in REF-08, out of `job_recovery.py` |
| `translate_project.py` | `translation/project.py`, `project_contracts.py`, `project_run.py`, `project_lease.py` | split |
| `translate_batch.py` | `translation/batch.py` |  |
| `batch_output_policy.py` | `translation/batch_output.py` |  |
| `memory_retrieval.py` | `translation/stages/memory_retrieval.py` | a pipeline stage, not knowledge |
| `translation_context.py` | `translation/context/model.py` |  |
| `context_execution.py` | `translation/context/execution.py` |  |
| `context_source.py` | `translation/context/source.py` |  |
| `review_application.py` | `review/application.py` |  |
| `review_exchange.py` | `review/exchange.py`, `exchange_model.py`, `exchange_parts.py`, `exchange_mapping.py`, `exchange_codec.py`, `exchange_files.py` | split by responsibility (REF-15) |
| `review_export.py` | `review/csv_export.py` |  |
| `review_import.py` | `review/csv_import.py` |  |
| `job_recovery.py` | `recovery/service.py` | resume orchestration, out of jobs. In REF-08 its pure reuse rules went to `resume_reuse.py`, because the translation pipeline uses them and sits below recovery |
| `job_recovery_store.py` | `recovery/store.py` |  |
| `legacy_translation.py` | `v1/translation.py` |  |
| `html_translate.py` | `v1/html_translate.py` |  |
| `translator.py` | `providers/libretranslate/client.py` | changed in REF-11: it is the LibreTranslate HTTP client, and the rich adapter uses it too, so it is not v1-only |
| `cache.py` | `v1/cache.py`; schema in `knowledge/cache/legacy_schema.py` | REF-18 R3 extracts the unchanged SQL; the runtime stays in v1 |
| `cache_legacy_format.py` | `knowledge/cache/legacy_format.py` | moved from v1 in REF-18 R3 |
| `cache_legacy_stream.py` | `knowledge/cache/legacy_stream.py` | moved from v1 in REF-18 R3 |
| `cache_admin_legacy_store.py` | `knowledge/cache/legacy_admin_store.py` | moved from v1 in REF-18 R3 |
| `resume.py` | `v1/resume.py` |  |
| `legacy_resume.py` | `v1/resume_scan.py` |  |
| `cli_contracts.py` | `interfaces/cli/contracts.py` | changed in REF-04: its errors went to `translation/errors.py` and `providers/errors.py`; the protocols only the CLI uses stay with the CLI. In REF-05 the `_Book*` request and outcome types went to `composition/cli.py`, the root that runs them |
| `cli_composition.py` | `composition/cli.py` |  |
| `desktop/bootstrap/composition.py` | `composition/desktop.py` |  |
| `cache_admin_composition.py` | `composition/builders/cache_admin.py` |  |
| `content_composition.py` | `composition/builders/content.py` |  |
| `glossary_catalog_composition.py` | `composition/builders/glossary.py` |  |
| `project_profile_composition.py` | `composition/builders/profiles.py` |  |
| `provider_composition.py` | `composition/builders/providers.py` | changed in REF-05: `CliProviderSession` went to `provider_session.py`, because `preflight` uses it; only the factory is composition |
| `review_composition.py` | `composition/builders/review.py` and sibling `review_files.py`, `review_context.py`, `review_packages.py`, `review_start.py`, `review_operations.py` | split in REF-15; the builder wires use cases, siblings own local execution |
| `translate_batch_composition.py` | `composition/builders/translate_batch.py` |  |
| `translate_project_composition.py` | `composition/builders/translate_project.py` |  |
| `cli.py` | `interfaces/cli/app.py` | split into `interfaces/cli/commands/` and `render.py` (REF-14) |
| `cli_progress.py` | `interfaces/cli/progress.py` |  |
| `desktop_bootstrap.py` | `interfaces/desktop/bootstrap.py` | `ayvu-desktop` entry point |
| `desktop/__init__.py` | `interfaces/desktop/__init__.py` |  |
| `desktop/adapters/__init__.py` | `interfaces/desktop/adapters/__init__.py` |  |
| `desktop/adapters/qt/__init__.py` | `interfaces/desktop/adapters/qt/__init__.py` |  |
| `desktop/bootstrap/__init__.py` | — | removed: the package only held the root, which moves to `composition/desktop.py` |
| `desktop/platform/__init__.py` | `interfaces/desktop/platform/__init__.py` |  |
| `desktop/presentation/__init__.py` | `interfaces/desktop/presentation/__init__.py` |  |
| `desktop/presentation/views/__init__.py` | `interfaces/desktop/presentation/views/__init__.py` |  |
| `desktop/resources/__init__.py` | `interfaces/desktop/resources/__init__.py` |  |
| `desktop/viewmodels/__init__.py` | `interfaces/desktop/viewmodels/__init__.py` |  |
