# Format compatibility map

This map lists where each current EPUB behavior lives today and what owns it under the
[format adapter contract](../decisions/format-adapter.md). #147 moved the CLI `inspect` and `extract`
flows to shared use cases. The v1 functions this map first listed as the current code were removed with
the v1 runtime in REF-18 step 6 (#286); each row now names the code that does the work today.

The production adapter is in `ayvu.formats.epub.adapter`, documented in [EPUB format](../decisions/epub-format.md).

`tests/formats/test_format_contract_architecture.py` checks that every behavior below is present, that every symbol
exists, and that jobs, retries and output paths are not assigned to a format.

| Behavior | Today | Owner | Note |
| --- | --- | --- | --- |
| inspect | `ayvu.formats.InspectContent` via `ayvu.composition.builders.content` | `ayvu.formats.service` and `ayvu.formats.epub.adapter.epub_format_adapter` | The typed result preserves path at presentation, title, authors, language, physical document/item counts and missing-value semantics; inspection creates no output |
| language detection | `ayvu.formats.epub.io.detect_epub_language` | `ayvu.formats.contracts.FormatInventory` | The inventory reports what the file declares. Falling back to `en` stays with the caller |
| extract | `ayvu.formats.ExportVisibleContent` via `ayvu.composition.builders.content` | `ayvu.formats.epub.adapter.epub_format_adapter` plus `ayvu.formats.markdown.writer.MarkdownDirectoryWriter` | The adapter returns ordered visible-body records; the writer preserves current filenames/text and atomically publishes each planned Markdown file |
| preview | `ayvu.interfaces.cli.preview._run_preview` | `ayvu.formats.epub.adapter.epub_format_adapter` | It renders results it is given. Choosing and translating them stays with the application |
| chapter selection | `ayvu.formats.epub.io.resolve_chapter_selection` | `ayvu.formats.contracts.ExtractionRequest` | The application resolves the selection and hands over logical keys |
| metadata | `ayvu.formats.epub.adapter.epub_format_adapter`, the metadata scope | `ayvu.formats.contracts.ContentScope` | A scope to extract and a fidelity aspect. Translating stays out of the adapter |
| navigation | `ayvu.formats.epub.io._navigation_document_entries` | `ayvu.formats.contracts.ContentScope` | Also a fidelity aspect |
| image alt text | `ayvu.formats.epub.html_structure.extract_html_document` with alt text included | `ayvu.formats.epub.html_structure.extract_html_document` | A scope for #144 to expose. Translating stays out of the adapter |
| review export | `ayvu.review.application.ExportReviewPackage` | #149 / `ayvu.review.local_packages.LocalReviewPackagePort` | Consumes the exact `TranslateProjectResult`, writes v2 through D106; the legacy writer remains only for v1 compatibility |
| v2 review package | `ayvu.review.export_review_package`, `ayvu.review.map_review_package` | `ayvu.review.local_packages.LocalReviewPackagePort` / `ayvu.formats.contracts.UnitTranslation` | Produces and validates stable-unit inputs; validation is read-only and application repeats it |
| review apply | `ayvu.review.application.ApplyReviewPackage` | `ayvu.review.local_operations.LocalReviewOperations` plus the EPUB `FormatReconstructor` | V1 inspection data is refused with `legacy_reexport_required`; v2 reconstructs in staging, validates the complete EPUB and publishes through D106 |
| rebuild | `ayvu.formats.epub.archive.EpubArchive` | `ayvu.formats.epub.archive.EpubArchive` | The adapter returns bytes and `ayvu.artifacts.service.ArtifactService` publishes them |
| validation | `ayvu.formats.epub.validation.validate_output_epub` | `ayvu.formats.epub.adapter.epub_format_adapter` | Warnings as text become `ayvu.formats.contracts.ValidationFinding` codes |
| archive preservation | `ayvu.formats.epub.archive.EpubArchive` | `ayvu.formats.epub.archive.EpubArchive` | Member order, names and untouched members stay inside the adapter, as a fidelity claim |
| mimetype storage | `ayvu.formats.epub.archive.EpubArchive` | `ayvu.formats.epub.archive.EpubArchive` | `mimetype` stays uncompressed, which the adapter must test |
| visible text selection | `ayvu.formats.epub.html_structure.extract_visible_text` for translation; `ayvu.formats.epub.html_structure.extract_body_visible_text` for Markdown export | `ayvu.formats.epub.html_structure` | #144 owns XHTML parsing and visibility selection; the Markdown writer does not parse markup |
| html reconstruction | `ayvu.formats.epub.html_structure.reconstruct_html` | `ayvu.formats.epub.html_structure.reconstruct_html` | #144 delegates to it, with no second segmenter |
| jobs | `ayvu.jobs.service.JobRepository` | `ayvu.jobs.service.JobRepository` | Not a format concern |
| resume | `ayvu.recovery.checkpoint.ResumeStateStore` | [`ayvu.recovery.service`](recovery.md) | Not a format concern |
| output path | `ayvu.kernel.OutputPlan` | #145 | Naming and overwrite rules stay with the application. `ayvu.formats.service.OutputGuard` only refuses the source |
| translation | `ayvu.composition.builders.translate_project.RichTranslationRuntime` | `ayvu.translation.engine.TranslationEngine` | Orchestration leaves the format layer in #145. An adapter never calls a provider. `ayvu.translation.document.DocumentTranslator` takes over after #153 |

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

## Enforced contracts

- [test_format_contract_architecture](../../../tests/formats/test_format_contract_architecture.py).
- [test_epub_archive_open](../../../tests/formats/epub/test_epub_archive_open.py).
- [test_epub_archive_copy](../../../tests/formats/epub/test_epub_archive_copy.py).
- [test_epub_format_reconstruction](../../../tests/formats/epub/test_epub_format_reconstruction.py).
- [test_html_structure_reconstruction](../../../tests/formats/epub/test_html_structure_reconstruction.py).
