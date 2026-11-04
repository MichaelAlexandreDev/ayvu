# Format compatibility map

This map lists where each current EPUB behavior lives today and what owns it under the
[format adapter contract](format-adapter.md). **Nothing has moved yet.** Every function in the
"Today" column keeps working unchanged until an issue migrates it explicitly.

`tests/test_format_adapter.py` checks that every behavior below is present, that every symbol
exists, and that jobs, retries and output paths are not assigned to a format.

| Behavior | Today | Owner | Note |
| --- | --- | --- | --- |
| inspect | `ayvu.epub_io.inspect_epub` | `ayvu.format_service.FormatInspector`, in #144 | `FormatInventory` has the same counts, title, authors and language |
| language detection | `ayvu.epub_io.detect_epub_language` | `ayvu.format_adapter.FormatInventory`, in #144 | The inventory reports what the file declares. Falling back to `en` stays with the caller |
| extract | `ayvu.epub_io.extract_markdown` | `ayvu.format_service.FormatExtractor`, in #144 | It returns units. Writing Markdown files belongs to the caller |
| preview | `ayvu.cli._run_preview` | `ayvu.format_service.FormatPreviewer`, in #144 | It renders results it is given. Choosing and translating them stays with the application |
| chapter selection | `ayvu.epub_io.resolve_chapter_selection` | `ayvu.format_adapter.ExtractionRequest` | The application resolves the selection and hands over logical keys |
| metadata | `ayvu.epub_io._translate_opf_title` | `ayvu.format_adapter.ContentScope`, in #144 | A scope to extract and a fidelity aspect. Translating stays out of the adapter |
| navigation | `ayvu.epub_io._navigation_document_entries` | `ayvu.format_adapter.ContentScope`, in #144 | Also a fidelity aspect |
| image alt text | `ayvu.html_translate._translate_image_alt_text` | `ayvu.html_structure.extract_html_document` | A scope for #144 to expose. Translating stays out of the adapter |
| review export | `ayvu.review_export.write_review_csv` | #149 | Maps units to review rows, and is not a format operation |
| review apply | `ayvu.epub_io.apply_reviewed_epub` | `ayvu.format_service.FormatReconstructor`, in #144 | Reading the CSV stays in `ayvu.review_import.read_review_csv` |
| rebuild | `ayvu.epub_io._copy_epub_with_replacements` | `ayvu.format_service.FormatReconstructor`, in #144 | The adapter returns bytes and `ayvu.artifact_service.ArtifactService` publishes them |
| validation | `ayvu.validation.validate_output_epub` | `ayvu.format_service.FormatValidator`, in #144 | Warnings as text become `ayvu.format_adapter.ValidationFinding` codes |
| archive preservation | `ayvu.epub_io._copy_epub_with_replacements` | #144 | Member order, names and untouched members stay inside the adapter, as a fidelity claim |
| mimetype storage | `ayvu.epub_io._copy_epub_with_replacements` | #144 | `mimetype` stays uncompressed, which the adapter must test |
| visible text selection | `ayvu.html_translate.extract_visible_text` | `ayvu.html_structure.extract_html_document` | #144 delegates to it |
| html reconstruction | `ayvu.html_translate.translate_html` | `ayvu.html_structure.reconstruct_html` | #144 delegates to it, with no second segmenter |
| jobs | `ayvu.job_service.JobRepository` | `ayvu.job_service.JobRepository` | Not a format concern |
| resume | `ayvu.resume.ResumeStateStore` | #146 | Not a format concern |
| output path | `ayvu.domain.OutputPlan` | #145 | Naming and overwrite rules stay with the application. `ayvu.format_service.OutputGuard` only refuses the source |
| translation | `ayvu.epub_io.translate_epub` | `ayvu.translation_document.DocumentTranslator` | Orchestration leaves the format layer in #145. An adapter never calls a provider |

## Differences #144 has to settle

- **Detection is new.** Today nothing detects a format: the code tries to open the file. An EPUB whose
  `mimetype` is not its first member opens today, and a strict signature would refuse it. #144 has to
  decide whether to declare a weaker signature, and has to test this case.
- **Text warnings become codes.** `validate_output_epub` writes warnings that name files. Moving to
  codes loses that detail unless #144 adds a locator to a finding.
- **`ebooklib` needs a file object.** `FormatSource.open` returns a read-only stream and no path. This
  works because `ebooklib` hands its argument to `zipfile.ZipFile`, which was confirmed against the
  installed version and a local EPUB.
