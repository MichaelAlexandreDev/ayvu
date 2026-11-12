# Approved translation memory

Implements [#161 / DESKTOP-130](https://github.com/DevStarrk1137/ayvu/issues/161).
Artifacts from #135, #138, #140 and #154 are used without changing their contracts
or published migrations. Retrieval integration in the pipeline and CLI is described below (#162).

## Contracts and storage

`knowledge/memory/approved/model.py` defines domain values and rules;
`knowledge/memory/approved/service.py` defines the `ApprovedMemoryRepository` port, commands
and `ApprovedMemory` use cases. `knowledge/memory/approved/store.py` implements the
production adapter `SqliteApprovedMemoryRepository` and its explicit bootstrap.
SQL and migrations remain in infrastructure.

Composition uses the already validated project directory from
`ProjectJsonRepository.resolve_database_paths(project_id).database.parent`:

```python
from ayvu.knowledge.memory.approved.service import ApprovedMemory
from ayvu.knowledge.memory.approved.store import open_approved_memory

repository = open_approved_memory(
    paths.database.parent,
    allowed_scopes=frozenset(authorized_scopes),
    provenance={project_id: provenance_repository},
)
memory = ApprovedMemory(repository)
```

`authorized_scopes` comes from trusted composition, never from document text.
Each supplied provenance repository implements the #138 port and resolves
references for that project. Cross-project reads require explicit source references
and authorized scopes; there is no automatic discovery of other databases.

The additional layout is:

- `projects/<UUID>/approved-memory.sqlite`: sensitive linguistic data;
- `projects/<UUID>/backups/approved-memory-schema/`: upgrade snapshots;
- `.approved-memory.sqlite.migrate.lock`: migrator lock in the same directory.

The `MEMORY_MIGRATIONS` registry is independent of `PROJECT_MIGRATIONS` and cache:
version 1 contains only infrastructure; version 2 creates `tm_revisions`, `tm_events`,
`tm_approvals` and `tm_operations`; version 3 adds only retrieval indexes by
scope/languages/source and linguistic key, preserving published checksums. The #135 runner controls creation, verification,
permissions, backups, limits, locking and upgrades. The project database remains at
version 4. Rich/v2 translation opens memory only with explicit `--translation-memory`.

There is no reading, copying, promotion or migration of `translations` or `cache_entries_v2`.
Cache cleanup, expiry and replacement do not reach this file. Memory deprecation
does not touch cache. Its retention is explicit: historical facts are not
purged automatically and the port provides no revision deletion. Administrative
removal of the entire database, with access stopped, is independent of cache.
Snapshots contain sensitive TM data and must be preserved privately alongside
the project's provenance references. They are not cache or training exports.
There is no dataset export, consent flag or sharing eligibility
inferred from approval.

## Identity and values

Each revision separately stores source project, structural document and revision,
#140 `unit_id` and `source_digest`, canonical visible source text,
search key, normalization/search versions, languages, target, kind, stage,
pipeline/profile/glossary revisions, scope and provenance references.
It stores neither entire documents, paths, raw locators nor provenance blobs.

`MemorySource.from_document(document, unit_id)` selects a real unit.
Units with `markup` spans require `visible_text=` supplied by the format
adapter's projection: stripping tags in the domain would lose `br` line breaks
or could include hidden content. Visible literals, identifiers and placeholders
remain in the projection. Units without markup use their original text and
refuse a divergent projection. Memory normalizes visible source to NFC;
the key uses NFC, casefold and whitespace collapse. Both policies store
algorithm and Unicode versions. Exact content identity remains that of the
model, even when two Unicode forms produce the same linguistic key.
Text and keys are neither structural identity nor approval evidence.

Linguistic source and target each accept at most 65,536 UTF-8 bytes; excess is
refused, without truncation, including before visible source normalization.
`repr`, logs and errors do not display values, keys, content hashes, credentials
or paths. Read results carry values for the authorized caller.

| Kind | Stage | Can receive human approval? |
| --- | --- | --- |
| `provider_response` | `provider` | No |
| `restored_visible` | `protected_restored` | No |
| `glossary_derived` | `terminology_applied` | No |
| `linguistic_final` | `linguistic_final` | Yes |

`linguistic_final` is the visible text after protected token restoration and all
selected deterministic linguistic transformations, including the effective glossary
applied after cache, before format reconstruction. Composition
must supply this value and matching evidence; the repository does not execute the
pipeline or convert an earlier value to final. `PipelinePin` requires immutable
`pipeline`, `profile` and `glossary` references, including a revision that
explicitly represents glossary absence. String equality with the #154
operational aggregate does not merge these identities.

## History and explicit review

The closed `TRANSITIONS` table authorizes only:

| State | Next states |
| --- | --- |
| `machine_candidate` | `human_edited`, `approved`, `rejected`, `deprecated` |
| `human_edited` | `approved`, `rejected`, `deprecated` |
| `approved` | `deprecated`, `superseded` |
| `rejected`, `deprecated`, `superseded` | None |

`create_candidate(CreateCandidate(...))` creates an entry and its first revision.
`record_human_edit(...)` creates another revision of the same entry: it records creation
and the human transition in the same transaction, without rewriting the previous revision.
`create_revision(DeriveRevision(...))` allows a new attempt or derivation from
any historical revision, always without inherited approval. This allows editing
an approved value or restarting after a terminal state without an illegal parent transition.
The `parent_revision` link, sequential number per entry and events
preserve concurrent branches. Source/language changes require a new entry;
target, glossary effect, scope or kind/stage changes require a new revision.

Before each command, the caller records a #138 event. The reference must
exist, be current, belong to the project/unit, carry the command's `operation_id`,
the three configuration references and required parents. Operations are
`tm_create`, `tm_derive`, `tm_edit`, `tm_approve`, `tm_reject` and `tm_deprecate`.
Editing and review require `ActorKind.HUMAN`; the #138 privacy decision to
record actor kind without naming the person is preserved.
An imported source requires an additional reference to `tm_import`, an explicit human
action, included in the parents of each new event in the imported chain.

`approve(...)` requires the exact reviewed `TargetValue`, scope, current revision
event version and `expected_approval` (current approval ID or `None`).
Even `machine_candidate -> approved` requires that human action, date and
provenance. Scores, storage, reuse and policies do not approve entries.
`reject(...)` and `deprecate(...)` also require explicit review and expected version.
`superseded` can only be produced by transactional approval replacement.

Values, events and operations have triggers against UPDATE/DELETE. The only
mutable projection is the current approvals index, unique by scope, structural/content
source, languages and stage. `BEGIN IMMEDIATE` checks versions and replaces
this index together with approval/supersession events. Two divergent concurrent
callers with the same expected approval produce one winner and one
`MemoryConflictError`, retaining candidates and their provenance. If failure occurs after
supersession, the entire TM transaction is rolled back. Contention exceeding the
writer wait limit also returns a typed conflict (`concurrent_write`), without
recording a partial operation; the caller may retry the exact command.

The external #138 event is recorded before the TM transaction: refusal or
crash may leave an attempted-action fact without TM application. There is no partial
approval or #138 payload duplication between databases. The operation ID and private
command hash allow exact replay with the original snapshot; reusing
the ID with different parameters is a conflict, including after later transitions.

## Scopes, queries and errors

`MemoryScope` requires a closed kind (`project`, `work`, `user`, `import`) and owner
UUID. Project scope must match the source project; work, user
and imported collection are explicit grants, never inferred from cache location
or identical text. Composition may authorize multiple owners.
Every query requires an explicit tuple of scopes contained in that authorization; direct
access outside it is `MemoryScopeError`. `current(...)` respects precedence in the
exact order of that tuple and compares only structural/content identities and language
pair, without key or text fallback.

`get`, `current`, `history` and `list_entries` exist in the service and port.
Listing uses an increasing sequence cursor, up to 256 revisions per page, optional
state/entry filter and explicit next cursor. History uses event version as a cursor,
also with a limit. State queries are resolved through SQL,
without loading the entire memory. Terminal states remain auditable but are never
returned by `current`. Typed errors distinguish missing entry, conflict,
invalid transition, denied scope, incompatible kind/stage, invalid provenance,
validation and storage failure; messages contain only fixed codes.

## Conservative retrieval (#162 / DESKTOP-131)

`MemoryQuery`, `RetrievalMode` and `RetrievalLimits` are domain contracts.
`ApprovedMemoryRepository.retrieve` is the pipeline's only query boundary;
`MemoryRetrieval` decides eligibility, similarity, ranking and application.
`DocumentTranslator` is the production segment pipeline available on this main
(the issue's historical name `SegmentTranslationPipeline` is not an existing class).
`RichTranslationRuntime` composes real adapters; there is no SQL reader in the pipeline
or claimed Desktop consumer.

The fixed order is exact v2 cache with full #153/#154 identity, exact approved
memory, approved fuzzy, opt-in cache compatibility, provider. Cache-only remains
exclusively exact. `RetrievalPolicy` is shared between application and CLI;
interface selectors do not calculate scores or confidence.

Only current `tm_approvals` pointers, with latest event `approved`, matching languages,
authorized scope, pinned search/normalization policies and
`linguistic_final`/`linguistic_final` enter approved retrieval. Exact means
NFC equality of the entire canonical source, including spaces, not `unit_id`,
content digest or casefold key equality. Fuzzy uses the existing deterministic Unicode
Indel ratio, over versioned NFC/casefold/whitespace. Concordance
searches for a substring of that key in the bounded set and never applies automatically.

The adapter uses indexes by scope/languages and source/search key. Approximate search
examines **at most 64 current rows per range and pin**, in the range of the key's first
Unicode code point, ordered by key/sequence; it sorts that set by length
distance, descending revision number and stable ID before materializing
values. This limits recall: it is not exhaustive substring or fuzzy search over all
memory. The application ranks automatic hits first, scopes in the caller's explicit
precedence, descending score, descending revision and stable IDs. Compatible
pins are queried before incompatible ones. The runtime authorizes only the
current project scope; the API accepts other explicitly authorized scopes.

Each query accepts 20 candidates/256 KiB by default, at most 64/1 MiB, up to
64 KiB UTF-8 of source and target individually. The caller may only lower these ceilings.
Invalid `N+1` fails during query construction, before the port. SQL checks
stored lengths before `_load`; the application also refuses ports that
exceed count or bytes. Snippets obey up to 512 UTF-8 bytes per side,
without splitting characters, and never replace the full value during application.

Default policy allows exact approved matches and keeps fuzzy as a suggestion. Explicit
conservative policy allows fuzzy with a threshold of at least 0.98 and exact structure.
Since #161 does not store a portable structural signature, the application requires checking
the source unit in the loaded document, including text/digest/revision, kind,
constraints, extensions, locale, context and protected terms. Units from another
document and markup receive no automatic application. Structure is not inferred from
a visible source that may have lost tags.

`plan_memory_pin(plan)` pins pipeline (revision and stages), profile (full semantic
identity) and glossary (immutable reference to ordered effective rules,
including explicit absence). Authors of final revisions must use those same
references from the real plan. A compatible final value skips the glossary; any divergent
pin produces only a `reviewed=False` suggestion, keeping the original `approved` state
as source information. Nothing derives or approves a revision automatically.

`LegacyCacheCompatibility` is a separate source, off by default. The
`--tm-legacy-cache-compatibility` option requires `--translation-memory` and rich runtime.
The source uses only v2 rows with complete scope/kind/fingerprint and integrity;
v1 rows without stage are excluded, even with opt-in. The legacy 0.95 threshold may
be used under this explicit policy. A cache result remains
`machine_candidate`/`reviewed=False`; score or reapplication does not change that. The adapter
does not write to the governed repository. Without a model label, the old CLI retains
its historical semantics; the new selectors are refused in that runtime.

`RetrievalDecision` distinguishes exact cache, approved memory, legacy cache and provider,
with full cache key or entry/revision, score, scope, state, confidence,
reason and sanitized provenance reference. `dataset_eligible` is always false.
Decisions are delivered per unit and persisted in the results artifact,
with explicit stage. Raw cache/provider/legacy values receive the glossary
exactly once. Decisions for these values remain unreviewed after derivation.
Artifacts including retrieval are auditable but are not reused as
raw: each operation revalidates approval/pins, including after deprecation. The
operational exact cache retains precedence regardless of TM state.

Suggestions generate `memory_review_suggestions`, forwarded to CLI and counted in the
existing report. Routine logs/events contain codes, not values, paths
or secrets. The authorized API returns full values and snippets for a
future concordance view. This issue adds no review editor, dataset,
consent or cache migration to approved memory.

## Enforced contracts

- [`tests/knowledge/memory/approved/test_approved_memory_lifecycle.py`](../../../tests/knowledge/memory/approved/test_approved_memory_lifecycle.py)
- [`tests/knowledge/memory/approved/test_approved_memory_scopes.py`](../../../tests/knowledge/memory/approved/test_approved_memory_scopes.py)
- [`tests/knowledge/memory/approved/test_approved_memory_provenance.py`](../../../tests/knowledge/memory/approved/test_approved_memory_provenance.py)
- [`tests/translation/stages/test_memory_retrieval_queries.py`](../../../tests/translation/stages/test_memory_retrieval_queries.py)
- [`tests/translation/stages/test_memory_retrieval_policy.py`](../../../tests/translation/stages/test_memory_retrieval_policy.py)
- [`tests/translation/stages/test_memory_retrieval_pipeline.py`](../../../tests/translation/stages/test_memory_retrieval_pipeline.py)
