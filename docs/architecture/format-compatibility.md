# Format compatibility map

This map lists where each current EPUB behavior lives today and what owns it under the
[format adapter contract](format-adapter.md). #147 moved the CLI `inspect` and `extract`
flows to shared use cases; legacy functions remain callable where other compatibility
paths still use them.

The production adapter is in `ayvu.epub_format`, documented in [EPUB format](epub-format.md).

`tests/test_format_adapter.py` checks that every behavior below is present, that every symbol
exists, and that jobs, retries and output paths are not assigned to a format.

| Behavior | Today | Owner | Note |
| --- | --- | --- | --- |
| inspect | `ayvu.content_use_cases.InspectContent` via `ayvu.content_composition` | `ayvu.format_service` and `ayvu.epub_format.epub_format_adapter` | The typed result preserves path at presentation, title, authors, language, physical document/item counts and missing-value semantics; inspection creates no output |
| language detection | `ayvu.epub_io.detect_epub_language` | `ayvu.format_adapter.FormatInventory` | The inventory reports what the file declares. Falling back to `en` stays with the caller |
| extract | `ayvu.content_use_cases.ExportVisibleContent` via `ayvu.content_composition` | `ayvu.epub_format.epub_format_adapter` plus `ayvu.markdown_writer.MarkdownDirectoryWriter` | The adapter returns ordered visible-body records; the writer preserves current filenames/text and atomically publishes each planned Markdown file |
| preview | `ayvu.cli._run_preview` | `ayvu.epub_format.epub_format_adapter` | It renders results it is given. Choosing and translating them stays with the application |
| chapter selection | `ayvu.epub_io.resolve_chapter_selection` | `ayvu.format_adapter.ExtractionRequest` | The application resolves the selection and hands over logical keys |
| metadata | `ayvu.epub_io._translate_opf_title` | `ayvu.format_adapter.ContentScope` | A scope to extract and a fidelity aspect. Translating stays out of the adapter |
| navigation | `ayvu.epub_io._navigation_document_entries` | `ayvu.format_adapter.ContentScope` | Also a fidelity aspect |
| image alt text | `ayvu.html_translate._translate_image_alt_text` | `ayvu.html_structure.extract_html_document` | A scope for #144 to expose. Translating stays out of the adapter |
| review export | `ayvu.review_application.ExportReviewPackage` | #149 / `ayvu.review_composition.LocalReviewPackagePort` | Consumes the exact `TranslateProjectResult`, writes v2 through D106; the legacy writer remains only for v1 compatibility |
| v2 review package | `ayvu.review_exchange.export_review_package`, `map_review_package` | `ayvu.review_composition.LocalReviewPackagePort` / `ayvu.format_adapter.UnitTranslation` | Produces and validates stable-unit inputs; validation is read-only and application repeats it |
| review apply | `ayvu.review_application.ApplyReviewPackage` | `ayvu.review_composition.LocalReviewOperations` plus the EPUB `FormatReconstructor` | V1 inspection data is refused with `legacy_reexport_required`; v2 reconstructs in staging, validates the complete EPUB and publishes through D106 |
| rebuild | `ayvu.epub_io._copy_epub_with_replacements` | `ayvu.epub_archive.EpubArchive` | The adapter returns bytes and `ayvu.artifact_service.ArtifactService` publishes them |
| validation | `ayvu.validation.validate_output_epub` | `ayvu.epub_format.epub_format_adapter` | Warnings as text become `ayvu.format_adapter.ValidationFinding` codes |
| archive preservation | `ayvu.epub_io._copy_epub_with_replacements` | `ayvu.epub_archive.EpubArchive` | Member order, names and untouched members stay inside the adapter, as a fidelity claim |
| mimetype storage | `ayvu.epub_io._copy_epub_with_replacements` | `ayvu.epub_archive.EpubArchive` | `mimetype` stays uncompressed, which the adapter must test |
| visible text selection | `ayvu.html_translate.extract_visible_text` for translation; `ayvu.html_structure.extract_body_visible_text` for Markdown export | `ayvu.html_structure` | #144 owns XHTML parsing and visibility selection; the Markdown writer does not parse markup |
| html reconstruction | `ayvu.html_translate.translate_html` | `ayvu.html_structure.reconstruct_html` | #144 delegates to it, with no second segmenter |
| jobs | `ayvu.job_service.JobRepository` | `ayvu.job_service.JobRepository` | Not a format concern |
| resume | `ayvu.resume.ResumeStateStore` | [`ayvu.job_recovery`](job-recovery.md) | Not a format concern |
| output path | `ayvu.domain.OutputPlan` | #145 | Naming and overwrite rules stay with the application. `ayvu.format_service.OutputGuard` only refuses the source |
| translation | `ayvu.epub_io.translate_epub` | `ayvu.translation_engine.TranslationEngine` | Orchestration leaves the format layer in #145. An adapter never calls a provider. `ayvu.translation_document.DocumentTranslator` takes over after #153 |

## Differences settled by #145

These books used to open and are now refused on every path, with a clear message, because each is a risk to the reader
or the machine, or is protected content:

- Duplicate members, including names that differ only by case or Unicode form.
- Member names with `..`, an absolute path, a drive letter or a backslash.
- A document declaring XML entities, and protected content in `encryption.xml`.
- A book past the adapter's size limits, or whose compression ratio looks like a decompression bomb.

**A defective `mimetype` is accepted and corrected.** It may be missing, not first, compressed or hold other
content. It carries no risk, so a copy writes it correct and first. Detection still does not believe such a file
from its extension alone, so a caller names the adapter.

**The adapter accepts two things the current flow does not:** a remote manifest item, which EPUB 3 allows, and a
processing instruction directly inside `metadata`. Both make `ebooklib` raise, and the preflight and the validator still
use `ebooklib`, so these books stay refused until those two move.

**Three differences in the output are deliberate:**

- A document with nothing to translate keeps its bytes. The current flow writes it again through a parser, which can
  add a declaration to an empty file.
- A translation holding characters XML cannot store loses only those characters. In the current flow the same text
  also loses its `&` and anything that looks like a tag.
- `alt` text holding such characters stays valid, where the current flow writes an invalid document.

**Text warnings stay text.** The report keeps naming the file, because the use case still runs `validate_output_epub`
behind its port. Moving to the adapter's findings loses which file, unless a finding gains a location first.
