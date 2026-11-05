# ADR: Provenance records and lineage

Status: `Accepted`

Date: 2026-11-03

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-014`, scoped to the record model, repository and storage of provenance

Related issue: [#138 — Record segment and artifact provenance](https://github.com/MichaelAlexandreDev/ayvu/issues/138)

## Decision and scope

A project keeps an append-only graph answering how an output was produced, stored in
the [project database](local-project-storage.md) as schema version 4. It records jobs,
operations, source kinds, producers and [artifacts](job-artifacts.md), and not the
content they processed.

`DocumentTranslator` writes provenance for provider, exact-cache and fuzzy-memory outcomes
when the rich path is used. The current CLI composition remains behind its named v1 adapter,
so it does not write these records. Review and validation are not instrumented. Later issues
may add those integrations, and they must measure the storage cost described below. Corpus,
datasets, quality scoring and telemetry are not accepted here.

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.provenance` | Domain | Record values, explicit unknowns, canonical serialization and typed errors |
| `ayvu.provenance_service` | Application | `ProvenanceRepository` port and query results |
| `ayvu.provenance_store` | Infrastructure | SQLite adapter and the schema SQL |

## The record

| Field | Meaning |
| --- | --- |
| `record_id`, `recorded_at`, `schema_version` | Assigned by the repository |
| `project_id`, `job_id` | Owner; the input fingerprint is read from the job and not copied |
| `operation`, `outcome_code` | Codes such as `translate` and `human_review`, or `approved` and `passed` |
| `actor_kind`, `source_kind` | What kind of thing acted, and where the result came from |
| `producer` | Provider, model, adapter and pipeline |
| `subject` | Optional opaque reference: namespace, version and value |
| `configuration_refs` | References to immutable configuration or policy revisions |
| `parents`, `supersedes`, `artifacts` | Graph edges and produced artifacts |
| `job_sequence`, `correlation_id`, `operation_id`, `idempotency_key` | Correlation |

**Actors are kinds, never people.** The record says `human` and holds no name or user
ID, because a durable history tying a person to what they read and edited would itself
be sensitive.

**Unknown is explicit.** All four producer fields are required, and each is an
identifier, `unknown` or `not_applicable`. There is no `reproducible` field.
`fully_identified` is false when any value is `unknown`, and even when true it only
describes what is known, because a known model does not promise the same result.

**Subjects are opaque.** They are supplied by the caller and compared exactly. Nothing
here generates or parses them, and current `c####-s####` review numbering is not an
identity: it can be stored only as one more opaque value under its own namespace.

## Append-only graph

Records are immutable. The repository has no update or delete operation, and triggers
refuse `UPDATE` and `DELETE` on all three tables. Records are retained with the
project and never pruned individually. Deleting a whole project is a separate lifecycle
operation that does not exist yet.

**Corrections.** A correction is a new record naming the old one in `supersedes`, with
the same subject. A record is corrected at most once, so the next correction names the
current version, and "current" is unambiguous: it is the record nothing supersedes.
Corrected records remain readable and appear in traversals with `current` false.

**Cycles.** Each write checks every reference inside one `BEGIN IMMEDIATE` transaction
and only accepts records that already exist, so the write path cannot form a cycle.
Traversal still detects one in a tampered database and raises `lineage_cycle` for the
records it collected.

**Idempotency.** An `idempotency_key` with identical canonical content returns the
stored record, ignoring `record_id` and `recorded_at`, because a legitimate retry
produces new ones. The same key with different content raises `idempotency_conflict`.

**Scope.** A repository serves one project. Every query filters by it, and a reference
to another project raises `cross_project_reference` with nothing written. Failures
never leave a partial record, because the record and its edges commit together.

## Bounded queries

| Query | Bounds |
| --- | --- |
| `list_by_job`, `list_by_subject`, `list_by_artifact` | `limit`, required, up to 1024 |
| `lineage`, `trace_artifact` | `max_depth` up to 256 and `max_records` up to 4096, both required |

Oversized and missing bounds fail. When a valid bound stops a query early, the result
says `truncated=True`, so a partial graph cannot be mistaken for a complete one. Order
is fixed for equal input. `trace_artifact` returns the producing records, their
ancestry and each job with its input fingerprint.

Reads use one read-only snapshot. A traversal that would take longer than the runner's
operation budget stops with a typed `deadline` error, and contended writes raise `busy`.

## Privacy

Every field is a bounded code, identifier or number, and encoding uses an explicit
allowlist. Spaces and path separators are rejected, which excludes sentences, paths and
URLs. Errors are bounded codes, and default `repr` output omits subjects, configuration
references and idempotency keys.

**Review rules the format cannot enforce:**

- A configuration reference names an immutable revision. An unkeyed digest of a
  low-entropy secret, prompt, glossary entry or source text must not be stored there,
  because it can be reversed by guessing.
- A subject value or code must not be derived from book text. A single word from a book
  passes the format check.

## Consequences and limits

- Metadata shares the project database and its 64 MiB limit. A synthetic run measured
  about 1.5 KiB of database per record with one parent and a subject, so roughly 44,000
  records reach the limit, before counting jobs and artifacts. A long book with several
  records per segment could get there, so the integration issues must choose granularity
  with this in mind.
- Records cannot be removed individually, which is deliberate and is also the cost.
- Only ancestors are traversed; there is no query from an input to everything derived
  from it.
- The schema has no production caller until later issues, so they are the first real
  test of the model.
- Released migrations cannot be edited, so a schema correction has to be a new version.
- Native Windows behavior and network filesystems have not been validated.
