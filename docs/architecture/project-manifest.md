# Local project manifest v1

The [accepted project decision](project-lifecycle.md) is implemented by
[#134](https://github.com/MichaelAlexandreDev/ayvu/issues/134).
This contract covers local EPUB project metadata, not durable jobs or migrations.

## Location and lifecycle

Translation creates or reuses
`<configured processing directory>/projects/<project UUID>/project.json`.
The default processing directory is `~/Documentos/Livros/Processando`.
The existing source and translated output stay in their selected locations.
The UUID is randomly allocated once, stored in the manifest, and survives restarts.

Direct, guided and batch translations resolve projects before translator
preflight. Cache-only reconstruction also resolves a project. Dry-run and the
separate preview path do not create project state. [`resume`](job-recovery.md) uses a
project only if its checkpoint recomputes to a job in one, and never creates one. A project created before a failed preflight remains
available for a retry.

Manifest removal never automatically deletes a source, output, cache, glossary,
or checkpoint. There is no automatic project cleanup, migration or deletion API.

## Schema and encoding

JSON is UTF-8, sorted by key, with compact separators and a trailing newline.
The maximum encoded size is 32 KiB. All fields below are required, including
nullable references and empty collections. Duplicate JSON keys, unknown/missing
fields, malformed values and unsupported versions are rejected. No automatic
upgrade or downgrade is performed.

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`; booleans are not integers for schema validation. |
| `project_id` | Canonical lowercase hyphenated UUID; new projects allocate UUID4. |
| `revision` | Integer from 1 through `2**63 - 1`, incremented by a successful update. |
| `source_path` | Normalized absolute source locator, at most 4096 characters; sensitive metadata, never identity alone. |
| `source_size`, `source_sha256` | Nonnegative byte count, bounded to 4 GiB, and lowercase SHA-256 of the original bytes. |
| `source`, `target` | Nonempty language identifiers, at most 35 characters, starting with an ASCII letter and containing letters, digits or hyphens. |
| `format_id` | `epub` for this version. |
| `profile_ref`, `provider_ref`, `policy_ref`, `glossary_ref`, `configuration_ref` | Null or opaque reference: lowercase kind of at most 32 characters, colon, and 64 lowercase hex digits. No resolved credentials or raw configuration. |
| `created_at`, `updated_at` | Timezone-aware ISO timestamps, at most 40 characters; updated time cannot precede creation. |
| `locations` | At most 16 distinct normalized project-relative paths, at most 240 characters each; empty by default. |
| `metadata` | At most 32 extension references keyed by `x-` plus 1–30 lowercase letters/digits/hyphens. Values are opaque references, not free text. |

Project-owned paths reject absolute paths, traversal, empty/dot components,
backslashes, colons, control characters, trailing dots/spaces and case-folded
duplicates. Storage also rejects symlinks, hardlinked files and aliases to the
source. The original cannot reside inside the managed projects workspace.

`ProjectRequest` and `ProjectManifest` are immutable values. Their default
representations omit source paths, fingerprints and configuration references.
Errors contain fixed codes rather than user paths, document content, provider
payloads or arbitrary filesystem exceptions. Manifest files themselves remain
private metadata and must not be shared as diagnostics without review.

## Standalone resolution and revisions

The repository fingerprints a regular source read-only and revalidates it inside
the workspace transaction. Symlink paths, including symlink ancestors, and
hardlinked sources are rejected. A source read is bounded to 4 GiB; glossary
fingerprinting is bounded to 1 MiB. Source mutation during fingerprinting fails.
Explicitly selected glossary links remain supported as in the legacy loader;
their target is read-only and only the content fingerprint is retained.

Compatibility includes the normalized source locator, content digest and size,
language pair, format and exact opaque selection references. The configuration
digest covers provider name/endpoint, chunk limit, metadata/alt-text selection,
chapter selection, fuzzy-memory settings, selected profile name and glossary
content digest. It stores neither raw selections nor glossary text. Provider
and profile references are compatibility markers, not implementations of future
provider/profile registries. `policy_ref` remains null until a policy owner
supplies an actual reference; this issue grants no network authorization.

Endpoints containing userinfo, query parameters or fragments are rejected before
generating persistent identity. Authentication needs a future credential-reference
adapter. Hash references are sensitive deterministic fingerprints, not encryption:
someone with manifest access can compare known candidate profiles or glossaries.

| Situation | Result |
| --- | --- |
| Exactly one compatible manifest | `created=False`, `reason=reused`, same UUID. |
| No project for the source | `created=True`, `reason=created`, new stored UUID. |
| Same unchanged source with different language/configuration | New UUID with `reason=new_configuration`; incompatible projects remain intact. |
| Multiple compatible manifests | `ambiguous_project`; no provider/output work. |
| Known source locator now has different content | `source_changed`; explicit source/project revision decision required. |
| Missing source, unsupported/corrupt manifest, unsafe path or storage failure | Distinct bounded error; dependent work stops. |

`ProjectRepository` exposes resolve, create, load and save. Resolve serializes
lookup/create. Explicit create permits a new configuration but rejects an
already-compatible project. Save checks the expected revision, preserves source
and selection identity and creation time, validates owned locations, increments
the revision and updates the timestamp. Two writers with one expected revision
cannot both succeed. The CLI run result carries the resolved `project_id`.

The additive [DEC-012 storage contract](local-project-storage.md) is exposed at
the privileged composition boundary by the concrete adapter's
`resolve_database_paths(project_id)`. It returns validated database/backup Paths
with a redacted repr and creates neither file. Those Paths are not added to the
framework-neutral application port or serialized into the v1 manifest. The
[migration protocol](project-migrations.md) consumes them explicitly.

## Atomicity, recovery and platform limits

The repository bounds the workspace to 1024 projects. A private workspace lock
serializes cooperating processes (POSIX `flock`, Windows `msvcrt` byte lock),
with a five-second acquisition deadline. New directories use mode 0700 and
temporary manifest/lock files use 0600 where POSIX permissions apply.
Existing POSIX workspace directories are restricted to mode 0700 before use;
failure to apply that restriction stops the operation. Windows
relies on the processing directory's OS ACL; mode bits do not establish ACLs.

Creation writes and fsyncs a validated manifest in a private staging directory,
then renames the directory into its final UUID location. Update writes/fsyncs a
same-directory temporary file and atomically replaces the manifest. POSIX
directory fsync follows publication; Windows has no equivalent implemented here.
Network filesystems and filesystems without the required locking/rename behavior
are not a validated persistence target.

Failure before publication preserves the prior manifest and cleans up this
invocation's temporary files. A killed process can leave `.staging-*` state;
`incomplete_project_creation` stops further resolution. With all Ayvu processes
stopped, preserve that staging directory by moving it outside `projects`, then
retry. Do not promote it manually or delete an original/output to repair state.
Corrupt manifests likewise require a preservation/recreation decision; they are
never silently discarded.

Failure to sync a directory after rename/replace returns
`project_commit_uncertain`. The complete new manifest may already be visible:
reload it before any revision update, or retry standalone resolution to reuse
its UUID. This outcome stops provider/output work and is not reported as success.
Atomic replacement guarantees complete old/new JSON, not that a disk or power
failure can always be rolled back. No backup or migration framework is claimed.

Static link/alias checks and no-follow file opens defend against unsafe input
paths. They do not sandbox a hostile process running as the same OS user and
swapping ancestor directories concurrently. The workspace must be trusted and
privately controlled. Process tests validate Linux locking and atomicity;
Windows paths have no native execution evidence in this change.
