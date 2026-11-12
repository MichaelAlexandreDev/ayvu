# ADR: Provenance records and lineage

Status: Accepted
Decision-Date: `2026-11-03`
Owners: Project maintainer (acceptance); Provenance (contract ownership)
Issue: [#138 — Record segment and artifact provenance](https://github.com/MichaelAlexandreDev/ayvu/issues/138)

Accepted: 2026-11-03 by the project maintainer, through explicit approval in the development session

Decision: `DEC-014`, scoped to the record model, repository and storage of provenance

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
| `ayvu.provenance.service` | Application | `ProvenanceRepository` port and query results |
| `ayvu.provenance.store` | Infrastructure | SQLite adapter and the schema SQL |

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

## Operational contract

The [module contract](../modules/provenance.md) contains the operational rules and
enforcement evidence. Historical implementation descriptions in this record refer to
the accepted #138 scope; later integrations do not expand this acceptance.
