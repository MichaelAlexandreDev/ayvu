# Inspect and visible-content export

Status: `Implemented`

Related issue: [#147 — Extract inspect and visible-content export application use cases](https://github.com/DevStarrk1137/ayvu/issues/147)

## Ownership

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.content_use_cases` | Application | Framework-neutral requests, results, capability negotiation, progress, cancellation and per-file outcomes |
| `ayvu.epub_format` | Format | Detects and inspects EPUBs; returns ordered visible body-text records |
| `ayvu.html_structure` | Format | Selects visible body text from XHTML without including the document head |
| `ayvu.markdown_writer` | Infrastructure | Validates the full destination plan and atomically publishes each Markdown file |
| `ayvu.content_composition` | Composition | Registers the production EPUB adapter and provides local sources and the Markdown writer |
| `ayvu.cli` | Interface | Renders the existing table, messages, paths, and exit status |

The application imports neither the CLI nor a concrete filesystem writer. It uses `FormatService` and a
`VisibleContentWriter` port. The registry is populated explicitly at the composition root; adapter selection comes
from bounded detection and `FormatDescriptor` claims. `EXPORT_VISIBLE_CONTENT` is a separate declared capability,
so a format that extracts translation units does not automatically promise visible-text export.

## Inspection

`InspectContent` asks the registry to detect the source, negotiates `INSPECT`, and returns the descriptor, typed
inventory, source revision, warnings, progress and a normalized failure. The inventory keeps the format version,
title, authors, language, document count and resource count. The EPUB inventory also reports the physical source
document count so the CLI's `Documents` field keeps its historical meaning even though the selection inventory
contains a synthetic OPF-title entry.

Inspection reads a bounded detection window, fingerprints the source, and lets the selected adapter inspect through
a read-only stream. It creates no destination, project, cache, job or artifact, and makes no provider or network
call. The CLI presents `Path`, `Title`, `Authors`, `Language`, `Documents` and `Items` with the same labels and
missing-value rendering as before. The source path is rendered only because the user selected it explicitly.

## Export planning and publication

`ExportVisibleContent` authorizes the selected output root before reading the source, negotiates the declared
visible-content capability, extracts records with one checked source fingerprint, validates an optional expected
revision, and asks the writer to plan every filename before publishing any content. The writer rejects invalid,
reserved, absolute or traversal-like names; canonical duplicates; symlink escapes; source aliases; and conflicts.
Existing non-empty directories still require `--overwrite`.

Each file is written to a private temporary file in the destination filesystem, flushed and checked, then published
atomically. With overwrite enabled, only the planned target is replaced. Unrelated files stay in place. Cancellation
is checked between documents and immediately before publication. Outcomes identify every known file as completed,
failed or not started; a later failure leaves earlier independent publications in place and marks the result as
partial. There is no all-or-nothing transaction for an arbitrary existing directory.

Markdown formatting remains the current one: document order, three-digit index, source stem, joined visible-text
fragments, trimmed lines, blank-line separators, and a final newline only for non-empty text. The adapter supplies
body-visible text; the writer does not parse EPUB or HTML. `PublishedFileRef` is an opaque, non-durable reference.

## Privacy and presentation

Default representations of requests, records, results and file references omit source paths and exported text.
Failures carry only a family and bounded reason code. Adapter/parser and filesystem exception messages are not
copied into results or logs. The CLI may show the exact source or destination path that the user selected, and in
developer mode shows the normalized reason code for inspection/export errors.

The source is rechecked before each publication. The first and the last hash it; those between compare its size,
because hashing the whole book per file made a 300-chapter, 200 MB book take 40 s instead of 1 s. A same-size change
is therefore caught at the last publication at the latest, and earlier files stay truthful as completed.

Files follow the umask, like the translated EPUB, and replacing a file keeps its mode. A filesystem without hard links,
such as a FAT or exFAT pen drive, gets an exclusive create followed by a replace: it still cannot overwrite a file,
but an empty file is visible for a moment.

The writer needs `dir_fd`, which Windows does not offer, so `extract` is not expected to work on Windows yet.

An export failure after at least one publication is presented with the completed count and the count that was not
published. The output directory can therefore contain completed files after a failed command; unrelated files are
preserved, and the message does not claim a directory transaction.
