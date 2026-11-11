"""Record the production and replay lineage of a document run."""
from __future__ import annotations

from typing import Any

import hashlib
import json

from ..artifacts import Artifact
from ..provenance import (
    UNKNOWN, ActorKind, ConfigurationRef, ContextProvenance, ProducerIdentity, ProvenanceRequest, SourceKind,
    SubjectRef,
)
from .document_contracts import UnitSource


_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)
_PROVENANCE_OPERATIONS = {
    UnitSource.EXACT_CACHE: ("reuse_exact_cache", SourceKind.CACHE, ActorKind.SYSTEM),
    UnitSource.APPROVED_MEMORY: ("reuse_approved_memory", SourceKind.TRANSLATION_MEMORY, ActorKind.SYSTEM),
    UnitSource.APPLIED_MEMORY: ("reuse_legacy_cache", SourceKind.CACHE, ActorKind.SYSTEM),
    UnitSource.PROVIDER: ("translate", SourceKind.PROVIDER, ActorKind.PROVIDER),
}


def _references(self) -> tuple[ConfigurationRef, ...]:
    plan = self._plan
    pairs = (("context", plan.context_ref), ("glossary", plan.glossary_ref), ("policy", plan.policy_ref))
    return tuple(ConfigurationRef(kind, reference) for kind, reference in pairs if reference is not None)


def _idempotency_key(self, run: Any, operation: str, extra: str = "") -> str:
    identity = json.dumps([run.context.job.job_id, run.document.revision_id,
                           self._plan.semantic_fingerprint or run.execution_nonce, operation, extra],
                          separators=(",", ":"))
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def _record_production(self, run: Any, artifact: Artifact, raw: list) -> None:
    """One record per kind of origin in this document. Which unit came from where is in the artifact."""
    document, job = run.document, run.context.job
    for unit_id, context in run.contexts.items():
        self._provenance.append(ProvenanceRequest(
            document.project_id, job.job_id, 'build_translation_context', ActorKind.SYSTEM, SourceKind.PIPELINE,
            self._plan.producer, subject=SubjectRef('translation_unit', 1, unit_id),
            artifacts=(artifact.artifact_id,), context_metadata=ContextProvenance(context.policy.value,
                tuple((e.kind.value, e.revision) for e in context.selected),
                tuple((o.source.kind.value, o.reason.value) for o in context.omitted), context.disclosure.utf8_bytes),
            idempotency_key=self._idempotency_key(run, 'build_translation_context', artifact.artifact_id + unit_id)))
    subject = SubjectRef("content_revision", 1, document.revision_id)
    parents = []
    for source, (operation, source_kind, actor) in _PROVENANCE_OPERATIONS.items():
        if not any(item[1] is source for item in raw):
            continue
        producer = _UNKNOWN_PRODUCER if source in (UnitSource.APPLIED_MEMORY, UnitSource.APPROVED_MEMORY) else self._plan.producer
        parents.append(self._provenance.append(ProvenanceRequest(
            document.project_id, job.job_id, operation, actor, source_kind, producer, subject=subject,
            configuration_refs=self._references(),
            idempotency_key=self._idempotency_key(run, operation, self._plan.post_processing_fingerprint))).record_id)
    self._provenance.append(ProvenanceRequest(
        document.project_id, job.job_id, "store_raw_results", ActorKind.SYSTEM, SourceKind.PIPELINE, self._plan.producer,
        subject=subject, parents=tuple(parents), artifacts=(artifact.artifact_id,), configuration_refs=self._references(),
        # A redone document stores a new artifact, which is a new fact.
        idempotency_key=self._idempotency_key(run, "store_raw_results", artifact.artifact_id)))


def _record_replay(self, run: Any, artifact: Artifact) -> None:
    document, job = run.document, run.context.job
    # Only the record that stored it: earlier replays would make each identical retry look different.
    producers = [node for node in self._provenance.list_by_artifact(artifact.artifact_id, limit=16).nodes
                 if node.record.request.operation == "store_raw_results"]
    self._provenance.append(ProvenanceRequest(
        document.project_id, job.job_id, "replay_raw_results", ActorKind.SYSTEM, SourceKind.PIPELINE, self._plan.producer,
        subject=SubjectRef("content_revision", 1, document.revision_id),
        parents=tuple(node.record.record_id for node in producers), artifacts=(artifact.artifact_id,),
        configuration_refs=self._references(),
        idempotency_key=self._idempotency_key(run, "replay_raw_results",
                                              f"{artifact.artifact_id}:{self._plan.post_processing_fingerprint}")))
