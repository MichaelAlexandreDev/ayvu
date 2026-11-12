# Refactoring baseline

Issue: [#266 — REF-01](https://github.com/MichaelAlexandreDev/ayvu/issues/266). Base commit: `5796e49`.

This is the starting point of the refactoring into the package structure decided in REF-02
([#267](https://github.com/MichaelAlexandreDev/ayvu/issues/267)). Each later refactoring pull
request quotes the same report, so progress shows as these numbers going down.

## How to reproduce

```bash
scripts/arch_metrics.py          # readable report
scripts/arch_metrics.py --json   # the same, as JSON
```

The script reads the source only: it does not import Ayvu and needs no network.
- Files inside a package belong to their first package segment.
- Top-level files belong to the module groups of the [module boundaries](decisions/modular-monolith-boundaries.md),
  listed in `LEGACY_GROUPS` inside the script.
- Two groups that import each other in both directions count as one cycle pair.

## Report at `5796e49`

```text
files: 116  lines: 37935
files over 400 lines: 30
files over 800 lines: 8
files over 1000 lines: 4
functions over 80 lines: 18

largest files (over 400 lines):
    4214 cli.py
    1069 review_composition.py
    1014 review_exchange.py
    1012 translation_document.py
     998 epub_io.py
     932 cache_admin_v2_store.py
     893 cache_identity.py
     890 translate_project.py
     701 review_application.py
     639 content.py
     629 project_store.py
     626 html_structure.py
     617 cache_schema.py
     605 cache_admin.py
     603 job_recovery.py
     598 translate_project_composition.py
     588 content_use_cases.py
     580 format_adapter.py
     520 artifact_store.py
     517 cache.py
     517 translation_context.py
     484 format_service.py
     482 translator.py
     467 approved_memory_store.py
     463 job_events.py
     460 project_profiles.py
     444 resume.py
     439 sqlite_migrations.py
     420 translation_engine.py
     414 provider_capabilities.py

longest functions (over 80 lines):
     330 cli.py:1717:_run_translation
     272 cli.py:827:translate
     156 cli.py:2072:_run_batch_translation
     155 cache_admin_legacy_store.py:63:import_cache
     136 content_use_cases.py:403:execute
     130 epub_io.py:212:translate_epub
     125 review_composition.py:594:begin
     115 cache_admin_v2_store.py:560:_preview_import
     109 cache_schema.py:465:prepare_cache
     102 cache_admin_v2_store.py:677:_execute_import
     101 cache_admin_v2_store.py:233:_export
      97 review_composition.py:436:_map_context
      96 cli.py:2282:_run_preview
      87 translation_context.py:431:_build
      86 glossary_catalog_store.py:284:_write
      82 cache_interchange.py:182:iter_records
      82 review_application.py:528:execute
      81 html_translate.py:76:translate_html

module groups that import each other: 12
  artifacts <-> formats
  artifacts <-> jobs
  content <-> formats
  formats <-> review
  formats <-> translation
  interfaces <-> jobs
  interfaces <-> providers
  interfaces <-> translation
  jobs <-> projects
  jobs <-> provenance
  jobs <-> translation
  knowledge <-> translation
```

## What the cycles come from

| Cause | Pairs |
| --- | --- |
| Resume (`job_recovery*.py`) lives in jobs but orchestrates projects, provenance, artifacts and translation | jobs ↔ projects, provenance, artifacts, translation |
| `cli_contracts.py` holds application errors and contracts used outside the CLI | interfaces ↔ jobs, providers, translation |
| `*_composition.py` files live inside modules and assemble parts of other modules | content ↔ formats, and part of review ↔ formats and knowledge ↔ translation |
| `html_structure` and `html_translate` use `translation_protection`; v1 uses EPUB internals | formats ↔ translation |
| `epub_io.py` reads and writes the review CSV | formats ↔ review |
| `memory_retrieval.py` is a pipeline stage that lives in knowledge | knowledge ↔ translation |
| `artifact_destination.py` calls `format_local` and `format_service` | artifacts ↔ formats |

## Frozen identities

`tests/test_persisted_identities.py` pins, independently of where the code lives:
- every literal domain tag that frames a persisted digest;
- the identity schema versions and the pipeline semantic revisions;
- a golden digest for each identity function, built from non-ASCII sample text;
- the names of the approved-memory command classes that enter `tm_operations.command_hash`.

A failure there means user data would be invalidated. Change such a value only on purpose, with a
migration and a version bump.
