# Standalone SQLite cache v2

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

Administration accepts explicit namespace selection, with preview and external map for v2 import. Without selection, the legacy barrier remains active. See [cache administration](cache-administration.md).

The repository does not infer execution references from texts or hashes.
Without an authenticated local execution binding, `resolve_pinned` returns
absence; cache-only remains without discovery, provider or transport. Compatible
resolution uses only authorized local v2 identities.
