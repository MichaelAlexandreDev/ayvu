# Cache administration and namespaces

`ayvu.knowledge.cache.admin` defines interface- and storage-independent contracts
for inspecting, previewing and executing cleanup, exporting and importing the
standalone cache. The cache reference and file references are opaque in
representations; results contain no entry texts. The summary identifies
the selected adapter and layout; on the legacy path, they are `legacy_v1` and
`sqlite_translations`, without declaring a schema version that does not exist in the database.

Cleanup requires explicit filters or `all_entries=True`. Preview produces an
immutable selection with count and digest. The repository must compare both under the
write lock and reject a stale selection before removing data.
Confirmation belongs to the interface calling the service. The execution count
is returned only after commit.

Inspection of a missing cache represents absence with `exists=False`; opening
for export must report absence without creating a cache or directory. Export
is an explicit operation containing original and translated texts. The file
is sensitive. Publication belongs to the file port and must use a restricted
temporary file in the same directory, atomic replacement and cleanup on failure
or cancellation.

Import fully validates JSON v1 and hashes before mutation. The
repository applies conflicts according to `replace` in a single transaction. Limits
are 512 MiB per file, 1,000,000 entries, 1,000,000 characters per text,
256 characters per metadata field, structure depth 16 and 20 structural
markers per entry. A smaller limit would refuse real caches: 90 thousand sentences already
exceed 64 MiB, and an old export of that size must remain importable. File limits must be enforced during
reading, not only after loading the content. Errors return a bounded family and
code, without external exception messages.

Cancellation points declared in `SAFE_POINTS` cover reading, intervals
between entries, start of mutation, commit and publication. Adapters call
`checkpoint` at the corresponding boundaries; the transaction or publication must be
aborted before confirmation when cancellation is observed. Progress contains
only phase and counts. The contract creates no API for browsing entries and does not
change v1 cache identity or format.

## Local compatibility

- An empty file, or SQLite without the `translations` table, is a cache without entries and is not changed. Only a file that
  is not SQLite results in `corrupt_cache`. Cleanup and its preview do not apply interchange limits because they read
  one row at a time and are the path to remove an oversized row.
- SQLite waits up to 30 s for a lock, like `TranslationCache`, because an ongoing translation holds the lock at each commit.
- The export temporary file is restricted while written. Once published, it follows umask or retains the replaced file's
  mode. On a system without hard links, such as a FAT or exFAT flash drive, publication without `--overwrite` uses exclusive
  creation followed by replacement: it still cannot overwrite, but an empty file is briefly visible.
- In a historical v1 format measurement, exporting and importing 150 thousand entries took 6.5 s and 10.4 s. These numbers do not measure v2 format.

## V2 namespace administration

Without namespace options, commands retain legacy v1 administration and its compatibility barrier. To choose a namespace, use `--namespace legacy-v1` or `--namespace v2 --scope-kind project --scope-id PROJETO --project-id PROJETO`. V2 kinds also include `local_user` and `work`; by specifying them, the operator explicitly authorizes the broader scope for the indicated project. Project identifiers must match for a `project` scope.

`inspect` and `export` accept `--all` to select all namespaces; `clean --namespace all --all` selects all for removal and still requires `--yes` or `--dry-run`. The old `clean --all`, without a namespace, remains limited to legacy behavior. `--source` and `--target` filters apply only to legacy entries; selections including v2 entries do not accept these filters. `--before` also selects v2 entries by lexical comparison of the canonical timestamp. Cleanup `--all` cannot be combined with filters.

V2 import requires `--scope-map ARQUIVO`, a JSON file external to the exported file, with a closed list of mappings. For example:

```json
[
  {"source": {"kind": "v2", "scope_kind": "project", "scope_id": "00000000-0000-0000-0000-000000004321"},
   "destination": {"kind": "v2", "project_id": "00000000-0000-0000-0000-000000004322", "scope_kind": "project", "scope_id": "00000000-0000-0000-0000-000000004322"}},
  {"source": {"kind": "legacy-v1"}, "destination": {"kind": "legacy-v1"}}
]
```

Each source must be explicitly mapped; source descriptions do not grant destination authorization. Check the preview and execute with the same map and destination cache:

```bash
uv run ayvu cache import export.json --cache destino.sqlite --scope-map mapa.json --dry-run
uv run ayvu cache import export.json --cache destino.sqlite --scope-map mapa.json
```

The second command generates a new preview and asks for confirmation, or accepts `--yes`. `--replace` must be specified to replace conflicts. `--all` is not allowed for import. V1 import without a map retains previous behavior and accepts neither `--dry-run` nor `--yes`.

The v2 envelope limits input and output to 1 GiB, 5 million entries, 10 thousand sections or mappings, a 1 MiB external map, 1024-byte UTF-8 references and depth 8. A legacy section retains the limit of 1 million v1 entries. The DESKTOP-123 row contract remains unchanged: identity, metadata and provenance have separate 64 KiB limits; source and value, 1 MiB; timestamp, exactly 27 ASCII bytes in UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` format. Exports, maps, preview temporary files (`.ayvu-cache-import-*.sqlite`) and recovery files may contain sensitive data: protect them like the cache and remove them when no longer needed. The CLI discards the temporary file after `--dry-run`, refusal or failure; after a completed import it is retained as an audit record. The CLI reports the JSON version actually emitted when the namespace is explicitly selected.


## JSON v2 interchange format

The top-level object is closed and contains `version` with integer value `2`, followed by `namespaces`, a list of sections. A section is also closed and has one of these forms:

- Legacy: `kind` equal to `legacy-v1` and `entries`. Each entry contains exactly `source_lang`, `target_lang`, `original_text_hash`, `original_text`, `translated_text` and `created_at`.
- V2: `kind` equal to `v2`, `scope_kind`, `scope_id` and `entries`. Each entry contains exactly `value_kind`, `semantic_fingerprint`, `source_digest`, `canonical_identity`, `source_text`, `value`, `metadata`, `provenance`, `created_at`, `provenance_digest` and `entry_digest`. `canonical_identity`, `metadata` and `provenance` are strings with the original UTF-8 bytes of JSON documents; bytes, including spaces, are preserved when reconstructing rows.

Order is mandatory for mapped interchange: `version` precedes `namespaces` at the top level, and all descriptor fields precede `entries` in each section. In mapped import of separate JSON v1, `version` precedes `entries`. Entry fields may arrive in any order but allow no extra or duplicate fields. Output uses deterministic order: row fields in schema order, legacy section first, v2 sections sorted by `scope_kind` and `scope_id` and rows sorted by each namespace's key. The CLI shows the version actually produced in exports with explicit selection.

V1 import without a map retains the old acceptance of free order, ignored unknown fields, UTF-8 BOM and UTF-16/32 JSON, now with incremental transcoding. Full validation precedes the transaction; duplicate JSON keys remain refused. The old repeated-entry and `--replace` policy also remains. This path uses private staging, with an on-disk digest index for unknown keys, a ceiling of sixteen times the v1 input limit plus 1 MiB and removal after the operation. Production composition does not use the old ports that load the entire payload. The v2 envelope remains strictly UTF-8.

The 1 MiB map limit also applies to the internal canonical representation: a sorted list of source and destination reference pairs, serialized as compact UTF-8 JSON. Each reference is a list `['legacy-v1']` or `['v2', tipo_de_escopo, id_de_escopo]`; this notation describes the internal representation, while the CLI-accepted file uses the objects in the example above and has its own 1 MiB raw limit. The map must cover exactly the sections present, with no omissions or extra sources. A file with only one scope requires only that scope's mapping; include the example's legacy row only if the file contains the legacy section. If the set exceeds any ceiling, export smaller explicit selections; no limit is raised automatically.

Mapped import requires an already existing destination database; v1 destinations accept only `legacy-v1 → legacy-v1`. It neither prepares nor migrates the destination automatically. The database journal mode must be `delete`, `truncate` or `persist`. Modes `wal`, `memory` and `off` are refused with `unsupported_audit_mode` before preview creation. Do not change the mode automatically to bypass refusal: prepare another v2 destination on supported storage through the API and consult the [DESKTOP-123 migration and recovery protocol](standalone-cache-v2.md).

A caller without CLI uses `preview_import`, passes the same preview to `execute_import` and calls `discard_import` when abandoning a preview. The revision is opaque and valid only in the process that issued it. After restarting, generate another preview. SQLite staging stays alongside the cache, with mode `0600` and a ceiling of four times the input limit plus 1 MiB; it is not part of the table queried by the runtime. The transaction commits cache and audit together. The audit records map and policy separately from entries, without changing provenance or date and without representing linguistic approval or consent. After an interruption, preserve files for analysis before discarding abandoned previews; the `committed` row in the `audit` table identifies a committed audit. Neither secure erasure nor protection against arbitrary processes ignoring the cooperative protocol is claimed.

Preview makes two batched parsing passes: the first checks the envelope and full map coverage before creating staging; the second prepares rows. Both check the digest of bytes actually consumed, in addition to revalidating the file, to refuse content changed during reading even when restored before the final check.

In explicit export, replacement also uses a restricted, incremental temporary copy of the previous destination to restore it if directory synchronization fails after rename. This copy obeys the configured output limit; a larger previous destination requires another output path. New files follow umask; replacements preserve previous permissions. No partial result is announced as success.
