"""Framework-neutral provenance repository contract and query results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .provenance import ProvenanceRecord, ProvenanceRequest, SubjectRef


@dataclass(frozen=True)
class LineageNode:
    """A record and whether a later record corrected it."""

    record: ProvenanceRecord
    superseded_by: str | None

    @property
    def current(self) -> bool:
        return self.superseded_by is None


@dataclass(frozen=True)
class ProvenanceGraph:
    """``truncated`` is True when a requested bound stopped the query before it finished."""

    nodes: tuple[LineageNode, ...]
    truncated: bool


@dataclass(frozen=True)
class JobInput:
    job_id: str
    input_fingerprint: str


@dataclass(frozen=True)
class ArtifactLineage:
    artifact_id: str
    producers: tuple[str, ...]
    graph: ProvenanceGraph
    jobs: tuple[JobInput, ...]


class ProvenanceRepository(Protocol):
    """Scoped to one project. Records are only ever appended; there is no update or delete."""

    def append(self, request: ProvenanceRequest) -> ProvenanceRecord: ...
    def get(self, record_id: str) -> LineageNode: ...
    def list_by_job(self, job_id: str, *, limit: int) -> ProvenanceGraph: ...
    def list_by_subject(self, subject: SubjectRef, *, limit: int) -> ProvenanceGraph: ...
    def list_by_artifact(self, artifact_id: str, *, limit: int) -> ProvenanceGraph: ...
    def lineage(self, record_id: str, *, max_depth: int, max_records: int) -> ProvenanceGraph: ...
    def trace_artifact(self, artifact_id: str, *, max_depth: int, max_records: int) -> ArtifactLineage: ...
