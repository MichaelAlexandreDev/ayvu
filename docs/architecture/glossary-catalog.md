# Managed glossary catalog

The guided glossary catalog is a local capability supplied by composition to
`glossary_catalog` through a repository. References identify catalog entries;
they do not authorize a root or grant access to arbitrary paths. Presentation
owns names, prompts, previews, confirmations and deliberate path display. The
explicit advanced `--glossary` loader and profile paths remain separate from
this catalog and keep their existing behavior.

`GlossaryLimits` bounds the catalog to 1,000 JSON files and each UTF-8 input or
canonical serialized output to 1 MiB. A glossary may contain at most 10,000
entries, with terms of at most 1,024 Unicode code points and translations of at
most 4,096. Simple string translations and the existing advanced rules are
accepted. Unsupported fields and invalid content are rejected as a whole. A
catalog above the file limit fails before parsing content. Entries that are
invalid, oversized, symbolic links, or files with multiple hard links are
represented as metadata statuses and cannot be read as content.

The local adapter uses descriptor-relative operations and refuses links in the
managed catalog. Creation uses an exclusive destination operation under a
cross-process lock. Editing checks a revision derived from the source bytes and
observed identity/version metadata. A changed or replaced file causes a stale
revision failure. If desired canonical bytes are already current and the revision
matches, the adapter returns `already-current` without rewriting. An external
byte-identical replacement with changed observable identity/version is stale. No
history can be inferred when both bytes and every observed identity/version field
are restored exactly.

An update revalidates the bytes, identity and revision under the lock immediately
before an atomic rename, which is the lock-scoped same-object revalidation the issue
allows. Two cooperating updates cannot both win. A process that ignores the lock could
still swap the file between that last check and the rename, because POSIX has no
conditional rename; that residual window is accepted. The directory is synced before
the rename, so a directory that cannot sync fails with the file untouched; if the sync
after the rename fails, the previous bytes are restored on a best-effort basis and the
operation returns failure. Creation writes a restricted temporary file and uses
exclusive publication, and the file and directory are synced.
Cancellation is checked before filesystem work and publication.
NFC/case-folded names share a conservative collision domain; filesystems may have
additional naming equivalences.

Glossary content, names, paths, references, revisions, digests and file identity
metadata are sensitive correlators. Results hide their values in default repr;
normalized failures contain codes rather than the original filesystem or parser
exception. Presentation may show local names and paths to the interactive user.
These use cases perform no provider, cache, translation-memory or network work.
