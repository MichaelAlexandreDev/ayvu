# EPUB format

The accepted scope and limits are recorded in the [decision](../decisions/epub-format.md).

The contract below preserves the implementation stage recorded in #144;
references to later integrations describe that stage, not their current delivery status.

## Reading

`ayvu.formats.epub.reader` is the public I/O adapter for `read_package`,
shared by both translation runtimes after REF-18 R1. Import it explicitly: `ayvu.formats.epub`
exports the HTML structure API without loading the archive reader. The values it returns,
`BookFacts` and `BookPackage`, live in the I/O-free `formats/book_facts.py` (exported by `ayvu.formats`).

Every check below runs before any unbounded read, and each failure is a bounded code that quotes nothing.
A defective `mimetype` is not among them: a copy writes it correct and first.

| Refused | Code |
| --- | --- |
| A name with `..`, an absolute path, a drive letter, a backslash, a NUL or a control character | `unsafe_member_name` |
| Two names equal after case and Unicode normalization | `duplicate_member` |
| A member marked as encrypted | `encrypted_member` |
| Compression other than stored or deflate | `unsupported_compression` |
| `container.xml` or the OPF missing or malformed | `missing_container`, `malformed_container`, `missing_package`, `malformed_package` |
| A manifest item missing from the archive, or an `href` leaving it | `manifest_member_missing`, `unsafe_manifest_href` |
| `encryption.xml` covering a document or the OPF, or using any other algorithm than font obfuscation | `encrypted_content` |
| An XML entity declaration, in any encoding | `entity_declaration` |

**Protected content stays protected.** Ayvu does not remove DRM, so such a book is refused, and obfuscated fonts,
which are not protected content, are left alone.

**Entities are refused whatever the parser would do.** The installed `lxml` already declines them, but
`pyproject.toml` allows older versions, in which this was not verified.

| Budget | Limit | Code |
| --- | --- | --- |
| Source file | 8 GiB | `source_too_large` |
| Uncompressed total | 4 GiB | `archive_too_large` |
| One member | 1 GiB | `member_too_large` |
| Members | 20,000 | `too_many_members` |
| One document read into memory | 64 MiB | `document_too_large` |
| OPF, `container.xml` or `encryption.xml` | 16 MiB | `document_too_large` |
| Compression ratio, above 1 MiB | 200:1 | `suspicious_compression` |
| Member name | 1024 characters | `member_name_too_long` |

These are judgment calls set well above what a normal book needs, not measurements, since only one real book was
available. A legitimate book above them is refused, so they may need to change.

## Detection

The signature is `mimetype` as the first member with its exact content, within the [bounded window](../decisions/format-adapter.md).
A file with the extension and without the signature is `no_match` with `signature_conflict`.
`inspect` and `extract` still try that one candidate, because a defective `mimetype` does not stop a translation
either: the adapter opens the book or reports why it cannot.

Detection reads only that. **A corrupt, unsupported or oversized file that passes it is reported by the first
operation**, whose failure has a family and a code: `resource`, `integrity`, `policy` or `input`.

## Content

| Scope | Becomes |
| --- | --- |
| `body` | Units from each XHTML document in the manifest that is not the navigation |
| `navigation` | Units from the navigation document |
| `metadata` | One unit for the OPF title, with an `epub_package` locator. A title holding a comment or element gives no unit, because translating its first text alone would leave the rest in the source language |
| `image_alt_text` | Alt text units, in the documents above. Asked for alone it is refused, since nothing would be read |

The current flow joins title and navigation under one flag, and the application can still ask for both.

A logical key is the member's path in the archive. Documents come in manifest order, as chapters are numbered today.
The inventory lists every key a selection can name, with the OPF first, so it counts one more than the XHTML documents.

## Reconstruction

1. Results and revisions are checked, and each XHTML document is rebuilt by `reconstruct_html`.
2. The title is changed, leaving prefixes, attributes, comments and CDATA. With no title result, the OPF keeps its bytes. Serializing still normalizes the XML declaration, whitespace outside the root, empty element pairs, character references and the encoding, which becomes UTF-8.
3. Members are copied in source order to a temporary file, changing only the ones above.
4. The result is opened again with the same checks, and a book with no document is `invalid_output`.
5. The bytes go to the artifact store, the source is fingerprinted again, and only then is the artifact published.

**The temporary file exists because a ZIP needs a seekable stream:** without one, `zipfile` sets flags on `mimetype`.
Up to 64 MiB it stays in memory. Above that it spills to an unnamed file with owner-only permissions, which holds the
book in plain text until it is closed.

**What is preserved.** Untouched members keep their uncompressed bytes, name, order, timestamp, attributes and
compression, and the archive keeps its comment. Compressed bytes can differ, because members are compressed again.
Fidelity is `preserved` in the sense of [HTML structure](../decisions/html-structure.md): as the parser reads a translated
document, not byte for byte.

## Publication

Files and the database cannot commit together, so a failure between them is reported truthfully and resolved by
[artifact reconciliation](../decisions/job-artifacts.md).

| Failure | Result | After the job's lease ends and reconciliation runs |
| --- | --- | --- |
| Before output, including validation and cancellation | Failure, and nothing published | Never ready |
| Before the rename | Failure | Abandoned |
| After the rename, before the commit | Failure | Ready, because the file is complete |

While the job holds a live lease, reconciliation leaves the record alone, because the job may still finish it.

An existing destination and whether to overwrite stay with the caller. `LocalOutputGuard` refuses the source and its aliases.

## Validation

| Found | Finding |
| --- | --- |
| No XHTML document | `no_documents`, error |
| A chapter body with no text or image | `empty_chapter`, warning |
| A link to a member that does not exist | `broken_internal_link`, warning |
| An image that does not exist | `missing_image`, warning |

Findings are counted and name no file, so the report says less than `validate_output_epub`. Links are unescaped first,
so `two%20words.xhtml` finds its member, unlike the current check, which reports it as broken.

## Enforced contracts

- [test_epub_archive_copy](../../../tests/formats/epub/test_epub_archive_copy.py).
- [test_epub_archive_open](../../../tests/formats/epub/test_epub_archive_open.py).
- [test_epub_format_contract](../../../tests/formats/epub/test_epub_format_contract.py).
- [test_epub_format_detection](../../../tests/formats/epub/test_epub_format_detection.py).
- [test_epub_format_extraction](../../../tests/formats/epub/test_epub_format_extraction.py).
- [test_epub_format_publication](../../../tests/formats/epub/test_epub_format_publication.py).
- [test_epub_format_reconstruction](../../../tests/formats/epub/test_epub_format_reconstruction.py).
- [test_epub_format_validation](../../../tests/formats/epub/test_epub_format_validation.py).
- [test_epub_io_paths](../../../tests/formats/epub/test_epub_io_paths.py).
