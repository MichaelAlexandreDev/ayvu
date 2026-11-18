# Upgrading from the v1 runtime

Ayvu used to translate with a runtime now called v1. Every new translation now runs on the rich
runtime: it needs a [model label](rich-runtime.md), records which provider and model produced each
text, and keeps its own cache. This page explains what happens to what v1 left behind.

## What changes

The v1 cache cannot say which provider, model or pipeline produced a translation, so the rich
runtime never reuses it. The first translation of a book after the upgrade translates every text
again, which can take as long as the first time did, and the wording may change if the provider or
model changed.

Before that work starts, Ayvu says so:

```text
Old translations: Ayvu's previous runtime left 1,234 texts for en→pt in .cache/traducoes.sqlite.
They are kept unchanged and are not reused: the translation starts again and is stored in
.cache/traducoes.sqlite.v2.sqlite. See "Upgrading from the v1 runtime" in the guide.
```

The notice appears in a translation, a dry run and a cache-only run (once for a whole batch), and only
when the cache passed with `--cache`, or the default `.cache/traducoes.sqlite`, has old texts for the
languages. Ayvu reads that file with SQLite's read-only mode, so it never creates, migrates or
changes it. If the file is in use, was interrupted, or is not a readable database, the notice says it
could not be read, and the translation goes on.

## Where the files are

| What | v1 | Rich runtime |
| --- | --- | --- |
| Cache | the `--cache` file, `.cache/traducoes.sqlite` by default | the same name plus `.v2.sqlite` |
| Progress of an interrupted run | `*.ayvu-state.json` in the processing folder | the project's job records |
| Translated EPUB | where you chose | where you choose; a new run needs its own destination or `--overwrite` |

Nothing is deleted or moved. The v1 files stay where they are until you remove them yourself.

## Finishing old jobs

An interrupted v1 job still resumes on v1, with or without a label, and so does an old standalone
checkpoint. The resume says so:

```text
Old job: this job started on Ayvu's previous runtime. It can be finished now; once that runtime is
removed, finish it with the frozen release v1-runtime-final or translate the book again.
```

The resume checkpoint table also shows `Runtime: previous (v1)`. Finish the jobs you care about
before you upgrade past the release that removes v1. After that, use the frozen release
`v1-runtime-final`, which is the last version with v1, in a separate checkout, with copies of the old
files.

## Keeping a copy

Before you remove anything, keep a copy of the old cache, the `*.ayvu-state.json` files and the EPUBs
v1 produced. Stop every Ayvu run first, so that the copy of the SQLite file is consistent. Reviewed
translations in old EPUBs are not carried over: compare them with the new output and review again
where they differ.

## Going back

To go back, check out `v1-runtime-final` in a separate folder, point it at your copies of the old
files, and keep its configuration apart from the current one. A rich job never resumes on v1.
