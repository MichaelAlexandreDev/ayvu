# Versioned project translation profiles

The #163 contract uses dependency owners #134 (manifest/CAS),
#152 (v1 preferences), #153 (raw cache semantic identity) and #157
(glossary reference/revision). No migration occurs during reading,
project opening or translation. The production pipeline does not yet consume the
snapshots or projections described here.

## Owners and closed schema

`projects/profiles/model.py` contains immutable values and v1 resolution policy;
`projects/profiles/service.py` contains history, ports and use cases. Both
are application contracts, without JSON, filesystem, SQLite, provider or interface.
The glossary reference still belongs to `knowledge.glossary.catalog`; it is not
replaced by a UUID, path hash or new identity registry.

Schema v1 requires exactly `version`, `target_language`, `glossary`, `style`.
The last three accept null. A present glossary requires exactly `reference`
and `revision`. Language preserves #152 validation and the 35-character limit;
style accepts null or `neutral`, `technical`, `literary`, `academic`. The managed
reference uses #157 validation and limits; revision is the exact
64-hex-digit token returned by it. There is no path, profile name,
extension or provider configuration field. Unknown fields and versions,
incorrect types and duplicate keys are refused in full.

Project references reuse #134 UUID identity. Profile
references are opaque `profile:` tokens accepted by #134, issued from randomness
independent of content, name, process and clock. A revision is the SHA-256 of the
versioned canonical content projection. The hash includes inert style to
record metadata changes without making them provider semantics.

Internal canonical serialization has tags: null `n`, booleans `t`/`f`, integer
`i<decimal>;`, UTF-8 string `s<bytes>:<conteúdo>`, sequence
`l<quantidade>:<itens>`, map `m<sequência de pares ordenados por chave>`.
Schema keys are fixed ASCII; legacy content is not renormalized.
The JSON adapter uses sorted keys, explicit null and rejects constants and
duplicates at all levels. No JSON enters the ports.

Audience, tone, formality, protected terms, work/character voice, chapter
context, format constraints and style activation are future additions to
versioned schema/policy. They are not inferred from the name, book or glossary.

## Explicit migration and compatibility window

Throughout the v1 window, `--profile`, `--target`, `--glossary` and guided mode
retain existing behavior. There is no new migration command. The legacy
consumer remains available in this version, without a removal date; closing the window
requires a future explicit, documented change. The application
consumer must supply processing root and managed root through
composition, the existing project, its expected revision and destination reference.
Example for an authorized caller with an already composed preferences repository:

```python
from ayvu.composition.builders.profiles import project_profile_service
from ayvu.projects.profiles.model import ProfileReference, ProjectReference

service = project_profile_service(processing_root, managed_glossary_root)
destination = ProfileReference.new()  # conserve este token para repetir a operação
result = service.migrate(preferences_repository, legacy_name,
    ProjectReference(manifest.project_id), manifest.revision, destination)
print(result.diagnostic())  # outcome tipado, sem dados privados
```

The caller must handle `migrated`, `already_migrated`, `conflict`,
`unresolved_glossary`, `unsupported_version`, `invalid_profile`, `cancelled`,
`persistence`, `not_found`, `invalid_plan`, `limit`. Only successful results
contain published state/snapshot. Do not discard refusals or assume success because
the profile validated. A source with unknown/read-only fields is refused.
There are no warnings returned and ignored by an interface.

The name selects a validated #152 entry without participating in identity.
The persisted receipt contains destination and initial revision, without name or path.
Repeating the same migration to the same destination returns `already_migrated`, including
after a later project edit: it does not undo that edit. Divergent content
for that destination returns conflict. A new destination requires the project's current
revision and a distinct explicit migration. The global source is neither saved nor deleted.

Relative glossary paths use only the injected managed root.
Absolute paths are accepted only when the canonicalized file is a direct
child of that root. The adapter queries #157, reuses its read rules and
lock and pins the exact revision. Absence, external path, case/NFC aliases,
symlink, multiple hard links, invalid file or stale revision block
migration with a closed reason (`missing`, `external`, `ambiguous`, `unsafe`,
`stale`, `io`) and without updating the project. Resolve the issue in the catalog and retry
explicitly. An external glossary remains usable through the legacy CLI
path; it receives no fictitious managed reference.

A null target preserves default intent. Resolution order is explicit
nonempty override, profile target, caller default (default `pt`). Guided
flow may supply its global default, as it already does today. Overrides stay in the
snapshot, never in the migrated profile. `glossary_present=False` inherits the glossary;
`True` selects the supplied reference and explicit null disables selection.
A present value with `glossary_present=False` is ambiguous and refused.

## Resolution, trusted facts and effects

`service.write` creates/updates a profile with expected project and profile
revisions. `service.resolve` requires these revisions, `PlanFacts` and `ProfileOverrides`;
`repository.read(project)` allows reading profiles, revisions and historical snapshots.
Resolve target and selection before supplying the exact projection of the planned request.
Do not construct a provider request from this contract in this issue.

A snapshot pins input project/revision, profile reference/revision/content,
explicit overrides, default, glossary/revision, v1 policy, plan facts and
full classification. Equal values and different map order produce the
same bytes and identity across processes. Publishing the snapshot advances the manifest
revision; the snapshot records the input revision before publication.
Immediately repeating its exact resolution returns the same snapshot, revalidating
glossaries and current profile revision. Later manifest changes require
new resolution or explicit history reading. Reading history does not require
the current glossary to retain the historical revision; creating a new snapshot
requires revalidating all bound references, including the overridden selection.

Only v1 policy derives classes; the caller supplies typed facts from the negotiated
plan: stage, exact request/capability projection and glossary stage.
These facts come from trusted composition, not from document, provider
response or adapter metadata. There is no field to select a class.
A request incompatible with target, unknown terminology support, missing
policy and incomplete classification fail before publication.

| Dimension | Plan | Class | Effect |
| --- | --- | --- | --- |
| Target language | translate/replay | raw-provider-affecting | Contributes to raw identity |
| Glossary | post_cache | downstream-only | Changes downstream replay/invalidation; retains raw contribution |
| Glossary | provider, with negotiated support and exact selection/policy | raw-provider-affecting | Reference, revision, capability and policy contribute before future lookup |
| Glossary | none | inert | Selection recorded, without effective contribution |
| Legacy style | all | inert | Metadata revision and snapshot change; request, raw output and cache do not |
| Execution stage | all | operational-only | Snapshot changes; raw semantic contribution does not |
| Target/glossary | inspect | inert | No request or lookup authorized by this plan |

`raw_semantic_contribution()` supplies bounded arguments for
#153 `knowledge.cache.identity_values.OpaqueSemanticRevision`. Its `profile_effect:` reference
is resolved by `state.raw_projection(reference)` to durable raw evidence
present in snapshots. It depends only on the raw partition and policy version;
never use the full snapshot hash in cache. Overall provider identity,
route and other policies still belong to #153 and are not replaced here.
`effects.downstream_projection()` delivers the separate partition. Names, paths,
glossary text, style, labels, inert overrides and operational stage do not
enter raw contribution. Sent terminology uses an opaque contribution
derived from effective reference/revision/policy, without exposing name or content.

There is no cache key/schema change, row migration, lookup or automatic
reconstruction. Cache continues storing raw translation; post-cache glossary
must be reapplied by future consumers. Future provenance must
store the historical snapshot reference, without reopening the current pointer and
retroactively assigning a new revision to an old artifact. Activating style
requires new policy/schema and explicit raw contribution, without reinterpreting v1.

## Publication, retention and privacy

`projects.profiles.store` uses the same workspace lock and CAS as #134. The manifest
remains v1: `x-translation-profiles` points to an immutable local record
`profiles:<digest>`. Original immutable project identity is unchanged.
The record stores heads, earlier revisions, receipts and snapshots; an update
is append-only. Limits: 64 profiles/receipts, 1,024 revisions and 1,024 snapshots per
project, 8 MiB per serialized record. Exhaustion returns `limit`, without truncation.

Private state files are prepared and synchronized before atomic manifest
replacement, with mode 0600 and directory 0700. Replacement preserves a
more restrictive previous manifest mode. Cancellation before publication,
stale revision or error retains the previous manifest. A sync error after rename
restores previous bytes and returns failure; as in #157, a persistently failing
filesystem prevents promising rollback durability. Processes ignoring
locks have the same residual POSIX window as #157 after the last revalidation.
There is no cancellation point between successful publication and return.

Prepared records that never received a pointer may remain orphaned after
failure; they are not read/published as success. This version neither collects garbage nor
removes historical revisions/snapshots. Future job/artifact references require
retention, not history mutation. All readers use the published pointer;
reading an unknown snapshot fails with `not_found`.

Local persistence contains necessary references/revisions, but no legacy
paths, glossary/book content, credentials, endpoint or profile name.
Repr, normalized exceptions and `diagnostic()` omit identifiers, revisions and
fingerprints. Canonical maps/bytes are private persistence/contract payload,
not diagnostics or support bundles. Authorized presentation may deliberately show the
legacy or local name; do not propagate them to default events or provenance.
These use cases neither emit events nor make translation calls.
