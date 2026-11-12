# Cache storage and administration

## Standalone SQLite cache v2

The local `knowledge/cache/schema.py` module is independent of the project executor. The
`prepare_cache(path, authorized_directory=None, checkpoint=None)` boundary classifies and prepares
a file before starting workers or opening the repository. `SQLiteExactCache`
implements `ExactCachePort` for the rich path. Production composition selects
this path when `translator_models.libretranslate` contains a valid label in
`config.json`; without a label it keeps legacy v1. The model is `declared:<label>`, declared
by the user, never observed on the server. `unknown` remains ineligible.
A new cache prepared at this boundary starts as v2.

The #252 integration derives the sibling file by appending `.v2.sqlite` to the
`--cache` name: `cache.sqlite` produces `cache.sqlite.v2.sqlite`. Original v1 is neither opened
nor changed by the rich runtime; there is no migration, copying, querying or promotion of its
rows. A v1 accidentally found at the sibling name is also refused. The first
rich use retranslates texts present only in v1. Removing the label restores
the legacy path on new runs, with its cache intact. Dry run and preview use v1.

The label accepts `[a-z0-9][a-z0-9_-]{0,47}` and is part of the semantic fingerprint.
**Changing the server model without changing the label reuses old results.**
The declaration persists in existing configuration and operational provenance/identity
fields, without appearing in repr, events, logs or checkpoints.
There is no model query or new HTTP discovery call.

In the #252 integration described here, the rich runtime prepares the sibling before any provider or worker, requires one worker
and refuses `--translation-memory`, `--cache-only` and `--require-full-cache`. Current offline
resolution does not pin the production identity and route needed for rich cache-only.
Concurrent runs encountering the exclusive preparation lock receive `cache_busy`
without calling the provider; no wait or automatic migration is added. Exact writes
continue using the participant protocol and #154 `put_if_absent`.

The version is recorded in the `knowledge.cache.schema` ledger; exact entries reside in
`cache_entries_v2`, with the `cache_entries_v2_compatible` index for compatible
resolution. In transactional migration, the v1 `translations` table is
preserved in the `legacy-v1` namespace: its rows are not copied to exact
entries because they lack sufficient semantic identity. An exact entry requires
authorized scope, value kind, semantic fingerprint and source content
digest. Concurrent writes use `put_if_absent`; value or
provenance conflict is reported without replacing the existing entry.
An identical repetition of value, metadata and provenance is idempotent. The
repository assigns `created_at` only to the winning insertion; the caller does not
supply this timestamp. `provenance_digest` references the operational provenance
projection, without inventing an approval or consent record.

## Limits and data

`CacheEntryV2Limits` is normative: canonical payload up to 64 KiB, source up to 1 MiB,
value up to 1 MiB, metadata up to 64 KiB and provenance up to 64 KiB. `created_at`
is exactly 27 ASCII bytes in UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` format.
Other limits are in UTF-8 bytes, separate from one another. An oversized entry
receives `not_cacheable`, without truncating fields or preventing normal translation.
Metadata accepts bounded flags and counts, without free text or credential,
endpoint, prompt or context fields. Texts, metadata, provenance, identities and hashes
are sensitive operational data. Their presence indicates neither translation approval
nor consent for corpus use.

## Locking, identity and backup

The cooperative protocol uses exclusive POSIX `flock` during `prepare_cache` and
shared locking during participant operations. SQLite's stronger exclusive lock
is also acquired before backup and DDL. The protocol coordinates
participants; it does not protect against arbitrary processes ignoring it. The lock
file's identity is also checked after acquisition and again before backup
and promotion, to detect replacement.

The file must be directly in `authorized_directory`, when provided. Symlink or
hardlink aliases, nonregular files and paths outside
the authorized directory are rejected. File identity is revalidated after the application
lock, after opening SQLite, before backup and before promotion. Without reliable
identity checks and protocol support, the operation fails closed;
in-place migration is refused without changing v1 or creating the parent directory. A
new empty v2 requires another destination on a supported POSIX platform and
filesystem. For example, in a new supported local directory:

```python
from pathlib import Path
from ayvu.knowledge.cache.repository import SQLiteExactCache

cache = SQLiteExactCache(Path("/caminho/local/suportado/novo-v2.sqlite"))
```

The v1 source remains intact; no row is transferred or promoted to an exact
entry. If no supported filesystem is available, v2 is unavailable: retain
v1 for reading or for the existing legacy execution.

The snapshot uses SQLite `VACUUM INTO` and flush, accounting for WAL;
never copy only the main file. Backup is verified before DDL and
retained after migration. Database, backup and auxiliary files may contain sensitive
operational content; do not expose them in logs. Migration and backup files are restricted to `0600`, and
directories created for them to `0700`. No secure data erasure is claimed.

## Durable phases and recovery

Local classification distinguishes `absent`, `empty`, `v1`, `v2`, `partial`,
`corrupt` and `foreign`. A `.migration` or `.migration-next` marker makes the
classification `partial`, including when a journal is pending after a crash. This
allows preparation to acquire locks and let SQLite recover the journal.

The `phase` field in each database's `.migration` JSON records the phases below. Each
checkpoint occurs after durable writing of the corresponding marker.
The `checkpoint(phase)` callback must raise an exception to request
cancellation; normal return allows continuation. Cancelling at `committed`
preserves committed v2 and leaves the marker for reconciliation.

| Phase | Predecessor | Cancellation or reopening |
| --- | --- | --- |
| `classified` | None | Database remains v1; on reopening, checks source, discards an interrupted `.v1-backup.sqlite.partial` snapshot and creates or verifies backup. |
| `backup-verified` | `classified` | Database remains v1; reopening rechecks backup and proceeds. |
| `migration-pending` | `backup-verified` | Database remains v1; reopening checks backup and starts transactional DDL. |
| `v2-built` | `migration-pending` | Before commit, rollback leaves v1; reopening rebuilds v2 in the transaction. |
| `integrity-verified` | `v2-built` | Before commit, rollback leaves v1 and reopening rebuilds; after commit, validates v2 and reconciles marker. |
| `committed` | `integrity-verified` | Committed v2 database; reopening checks backup and legacy, reconciles marker and does not reinsert data. |

In recovery before commit, DDL runs again within a
transaction; already recorded checkpoints are skipped, without moving the marker backward. Locks
and transaction are transient gates, not durable phases. Backup is retained and
never overwrites the source.

`.migration-next` indicates interrupted marker publication and causes
`cache_recovery_required`: preserve all files for manual analysis. A
preexisting unrecorded backup or partial snapshot causes `cache_backup_exists`; a missing,
inconsistent or changed backup may cause `cache_backup_invalid`. An invalid,
incompatible marker or one without verifiable identity causes `cache_recovery_required`.
Keep copies of all files, including database, WAL, journal, markers and
backup, and consult someone able to verify source and backup integrity
and identity. Do not delete markers or blindly replace the sole copy. After
resolving the condition, retry with the source locked, or choose
a new empty v2 destination on supported storage.

## Administration

Administration accepts explicit namespace selection, with preview and external map for v2 import. Without selection, the legacy barrier remains active. See [cache administration](#cache-administration-and-namespaces).

The repository does not infer execution references from texts or hashes.
Without an authenticated local execution binding, `resolve_pinned` returns
absence; cache-only remains without discovery, provider or transport. Compatible
resolution uses only authorized local v2 identities.

## Cache administration and namespaces

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

Mapped import requires an already existing destination database; v1 destinations accept only `legacy-v1 → legacy-v1`. It neither prepares nor migrates the destination automatically. The database journal mode must be `delete`, `truncate` or `persist`. Modes `wal`, `memory` and `off` are refused with `unsupported_audit_mode` before preview creation. Do not change the mode automatically to bypass refusal: prepare another v2 destination on supported storage through the API and consult the [DESKTOP-123 migration and recovery protocol](#standalone-sqlite-cache-v2).

A caller without CLI uses `preview_import`, passes the same preview to `execute_import` and calls `discard_import` when abandoning a preview. The revision is opaque and valid only in the process that issued it. After restarting, generate another preview. SQLite staging stays alongside the cache, with mode `0600` and a ceiling of four times the input limit plus 1 MiB; it is not part of the table queried by the runtime. The transaction commits cache and audit together. The audit records map and policy separately from entries, without changing provenance or date and without representing linguistic approval or consent. After an interruption, preserve files for analysis before discarding abandoned previews; the `committed` row in the `audit` table identifies a committed audit. Neither secure erasure nor protection against arbitrary processes ignoring the cooperative protocol is claimed.

Preview makes two batched parsing passes: the first checks the envelope and full map coverage before creating staging; the second prepares rows. Both check the digest of bytes actually consumed, in addition to revalidating the file, to refuse content changed during reading even when restored before the final check.

In explicit export, replacement also uses a restricted, incremental temporary copy of the previous destination to restore it if directory synchronization fails after rename. This copy obeys the configured output limit; a larger previous destination requires another output path. New files follow umask; replacements preserve previous permissions. No partial result is announced as success.

## Enforced contracts

- [`tests/knowledge/cache/test_cache_repository_v2.py`](../../../tests/knowledge/cache/test_cache_repository_v2.py)
- [`tests/knowledge/cache/test_cache_schema_v2_migration.py`](../../../tests/knowledge/cache/test_cache_schema_v2_migration.py)
- [`tests/knowledge/cache/test_cache_schema_v2_snapshot_recovery.py`](../../../tests/knowledge/cache/test_cache_schema_v2_snapshot_recovery.py)
- [`tests/knowledge/cache/test_cache_schema_v2_worker_gate.py`](../../../tests/knowledge/cache/test_cache_schema_v2_worker_gate.py)
- [`tests/knowledge/cache/test_cache_admin.py`](../../../tests/knowledge/cache/test_cache_admin.py)
- [`tests/knowledge/cache/test_cache_admin_transactions.py`](../../../tests/knowledge/cache/test_cache_admin_transactions.py)
- [`tests/knowledge/cache/test_cache_admin_v2_contracts.py`](../../../tests/knowledge/cache/test_cache_admin_v2_contracts.py)
- [`tests/knowledge/cache/test_cache_admin_v2_roundtrip.py`](../../../tests/knowledge/cache/test_cache_admin_v2_roundtrip.py)
- [`tests/knowledge/cache/test_cache_interchange_v2_validation.py`](../../../tests/knowledge/cache/test_cache_interchange_v2_validation.py)
- [`tests/knowledge/cache/test_cache_interchange_v2_streaming.py`](../../../tests/knowledge/cache/test_cache_interchange_v2_streaming.py)
