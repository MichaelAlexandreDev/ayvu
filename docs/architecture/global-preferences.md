# Global preferences use cases and JSON v1 compatibility

This contract implements [#152](https://github.com/MichaelAlexandreDev/ayvu/issues/152)
within the [accepted modular boundaries](modular-monolith-boundaries.md).
It does not accept a new architecture decision or change project storage.

## Ownership and consumers

[`preferences.py`](../../src/ayvu/preferences.py) owns immutable preference values
and validation. [`preferences_service.py`](../../src/ayvu/preferences_service.py)
owns `GetPreferences`, `UpdatePreferences`, structured results and the repository
port. Neither imports JSON, paths, filesystem clients or presentation libraries.
[`preferences_store.py`](../../src/ayvu/preferences_store.py) implements the v1
JSON adapter. [`config.py`](../../src/ayvu/config.py) preserves the legacy config
objects, path derivation and compatibility facade for existing consumers.

The [field classification](global-preferences-fields.json) is executable contract
data: tests compare every serialized field, including all five folder names and
all optional profile fields, with its owner, validation, sensitivity, default,
compatibility rule and consumer. Legacy named profiles remain a separate category
inside the same v1 file; no versioned profile snapshot is introduced.
Project identity/settings, window state, provider connections, credentials and
network-policy values cannot become fields of these DTOs. Unknown fields never
enter a known-field serialization payload.

## Query and update outcomes

`get_preferences(repository, GetPreferences())` returns a snapshot containing
known values, schema information, an opaque revision, existence/read-only state,
bounded warning codes and a typed error when loading fails. A missing file returns
v1 defaults and revision `missing`. GET creates no directory or lock file and
never normalizes or rewrites an existing file.

The revision is SHA-256 of the exact loaded bytes. It is a private concurrency
token, not a public identity or permission grant, and is absent from default repr.
No revision field is added to JSON v1. Edits to whitespace also invalidate the
previous revision. There is no automatic merge after a conflict.

`update_preferences(repository, UpdatePreferences(values, expected_revision))`
validates the complete intent and returns effective values, a distinct persisted
flag, the new snapshot on success and a typed failure otherwise. Invalid request
types/revision syntax are rejected at construction. Repository errors use fixed
codes/allowlisted field names; raw exceptions are not carried in result DTOs.

Under one exclusive lock, the adapter reads current bytes, refuses unknown fields,
compares the expected revision and publishes a validated complete JSON payload.
Two writers using the same revision cannot both save. A first-use writer expecting
`missing` cannot overwrite a file another process created during the prompts.

## Existing v1 files and validation

The location remains `$XDG_CONFIG_HOME/ayvu/config.json`, falling back to
`~/.config/ayvu/config.json`. Version 1, defaults, relative glossary paths, reader
command meaning, all five folder roles and the exact optional named-profile fields
remain preserved. `style` remains inert translation metadata.

Known missing fields receive existing defaults. Same-version unknown top-level,
folder or profile fields produce `unknown_fields` and a read-only known-field
view. Their raw values are neither copied into a DTO nor logged. Every update is
refused while those fields remain: the file must be reviewed and adjusted
explicitly before saving, so an older application cannot silently drop future data.

JSON is bounded to 128 KiB. Duplicate keys, non-JSON constants, malformed data,
wrong types and newer versions have explicit errors. At most 64 named profiles
are allowed. Language identifiers have 1–35 ASCII letters/digits/hyphens/underscores
and start with a letter. Profile names have at most 128 characters; path strings
4096; reader commands 1024; folder names 128. Folder names cannot be paths, dot or
parent traversal. Strings reject control characters and invalid UTF-8.
Legacy surrounding spaces in language values are retained; identifier validation
uses the trimmed form. Application consumers retain their existing normalization.

Malformed, newer or over-limit files are preserved and are never replaced by
saving the fallback defaults from Settings. To recover, keep a private copy of the
existing file outside the active config location, then explicitly correct its
syntax/version/known values and reduce any over-limit data before reloading.
Unknown fields must likewise be reviewed rather than silently discarded.
No new reset/repair command or automatic truncation is introduced. These documented
bounds also apply to legacy v1 files that the old unbounded reader accepted.

Credential option/assignment syntax (including token, API key, password, secret
and authorization, including compound options such as `--client-secret` and
`--bearer-token`) and URL userinfo are rejected in known string values. New
credential-bearing keys are not permitted by the schema. This is a closed local
preference format, not authentication or a secret store. Authorized path/reader/
glossary fields necessarily remain in the explicit local persistence payload;
they are excluded from DTO repr and default diagnostics. Do not serialize a
preference payload into a diagnostic log or share the file as a support bundle.

## CLI compatibility and effective session state

First-use and Settings prompts remain owned by the CLI, which uses the shared
get/update boundary through the compatibility facade. Existing default and
malformed-config warnings remain presentation decisions. Unknown fields add a
read-only warning; a failed save never prints `Settings saved.`

On a storage error or stale revision, validated edits can remain effective in the
current session while persistence is explicitly reported as failed. The original
revision is retained; later edits in that session cannot overwrite a concurrent
writer. Reload the configuration and make a fresh merge decision before retrying.
Invalid input keeps the prior effective settings instead. A successful save
advances the session's revision for the next edit.

Legacy `ConfigStore.load()` returns the same config shape. `save()` now requires a
revision obtained by loading with that instance or carried by a loaded config;
without one it is a create-only attempt against `missing`. A fresh facade cannot
silently overwrite an existing file. New consumers should use explicit requests
and structured results rather than the compatibility methods.

## Publication and resource limits

Updates use a 0.5-second bounded workspace lock (POSIX flock or Windows byte lock)
and a same-directory private temporary file, flush, fsync and atomic replace.
An existing file's restrictive owner read/write permissions are retained, with
new files using mode 0600 on POSIX. Reads reject nonregular files, symbolic links
and hardlinks. POSIX writes reject ancestors owned by another user or writable by
other users without sticky-directory protection; user folder modes are not changed.

Failure before replace leaves the prior bytes intact and removes only this
operation's temporary file. POSIX directory-fsync failure after replace returns
`commit_uncertain`: complete new bytes may already be visible, so reload before
retrying. Atomicity preserves complete old/new JSON; it does not promise rollback
after hardware failure. There is no backup deletion, new database or migration.

Locks serialize cooperating application processes. The configuration directory
must be trusted; pathname checks are not a sandbox against a hostile process
running under the same OS user. Windows ACLs, junctions and native byte-lock
behavior have not been validated by the Linux tests. Network filesystems are not
a validated target for these locking/rename guarantees.
