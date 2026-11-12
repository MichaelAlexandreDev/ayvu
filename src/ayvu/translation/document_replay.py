"""Read and prove reusable raw results for the current document run."""
from __future__ import annotations

from typing import Any

from ..artifacts import Artifact, ArtifactError, ArtifactIntegrityError, ArtifactPathError
from .resume_reuse import REUSE_DIMENSIONS, ArtifactIntegrity, ResumeStage, StageEvidence, select_resume_stage
from ..kernel import JobWarning
from .document_contracts import DocumentTranslationResult, TranslationExecutionError
from .raw_results import (
    RAW_RESULTS_MEDIA_TYPE, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, _RAW_RESULTS_VERSIONS, _json_object,
    reuse_dimensions,
)


_MAX_ANCESTOR_ARTIFACTS = 1024


def _replay(self, run: Any) -> DocumentTranslationResult | None:
    """Reuse raw results only when every identity and integrity check passes."""
    if self._plan.semantic_fingerprint is None:
        return None
    request = self._artifact_request(run.document, run.context.job)
    found = self._artifacts.find_reusable(request)
    if found.artifact is None:
        if found.reason != "artifact_not_found":
            # A damaged ready record would block new work forever, so withdraw it. Its file stays.
            self._artifacts.withdraw_corrupt(request)
        return self._replay_from_chain(run)
    artifact = found.artifact
    try:
        with self._artifacts.open_reusable(artifact.artifact_id, request) as stream:
            payload = stream.read()
    except (ArtifactIntegrityError, ArtifactPathError):
        self._artifacts.retire_if_corrupt(artifact.artifact_id)
        return self._replay_from_chain(run)
    raw = self._decode_raw(payload, run.document, run.cache_scope)
    results = []
    for index, unit in enumerate(run.document.units):
        run.checkpoint("pipeline_before_unit")
        results.append(run.apply_raw(unit, raw[index]))
        run.progress(index + 1)
    self._record_replay(run, artifact)
    return run.result(tuple(results), artifact.artifact_id)


def _chain_artifacts(self, project_id: str, job_id: str) -> dict[str, Artifact]:
    """The newest ready raw-results record of each document in one earlier job. Read only, once per job."""
    known = self._ancestors.get(job_id)
    if known is not None:
        return known
    found: dict[str, Artifact] = {}
    try:
        artifacts = self._artifacts.list_ready(job_id, limit=_MAX_ANCESTOR_ARTIFACTS)
    except ArtifactError:
        artifacts = ()
    for artifact in artifacts:
        request = artifact.request
        if ((request.project_id, request.job_id, request.role, request.stage, request.media_type)
                != (project_id, job_id, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, RAW_RESULTS_MEDIA_TYPE)):
            continue
        payload = self._read_chain_payload(artifact)
        data = _json_object(payload)
        if data is not None and type(data.get("document_id")) is str:
            found[data["document_id"]] = artifact  # Oldest first, so a redone document keeps its newest record.
    self._ancestors[job_id] = found
    return found


def _read_chain_payload(self, artifact: Artifact) -> bytes | None:
    """Verified bytes of an earlier job's record, or None. That job is ended and its records are never changed."""
    try:
        with self._artifacts.open_reusable(artifact.artifact_id, artifact.request) as stream:
            return stream.read()
    except (ArtifactIntegrityError, ArtifactPathError, ArtifactError, OSError):
        return None


def _replay_from_chain(self, run: Any) -> DocumentTranslationResult | None:
    """Reuse an earlier job's raw results only when ``select_resume_stage`` proves every dimension equal."""
    reusable = run.context.reusable_jobs
    if not reusable:
        return None
    document = run.document
    for job_id in reusable:
        artifact = self._chain_artifacts(document.project_id, job_id).get(document.document_id)
        if artifact is None:
            continue  # This job never finished this document; an earlier one may have.
        payload = self._read_chain_payload(artifact)
        data = _json_object(payload)
        schema_valid = (data is not None and type(data.get("schema_version")) is int
                        and data["schema_version"] in _RAW_RESULTS_VERSIONS and data.get("document_id") == document.document_id)
        recorded = data.get("dimensions") if schema_valid else None
        produced = (tuple((name, recorded.get(name)) for name in REUSE_DIMENSIONS)
                    if type(recorded) is dict and all(type(value) is str for value in recorded.values()) else ())
        evidence = (
            StageEvidence(ResumeStage.TRANSLATE,
                          ArtifactIntegrity.CORRUPT if payload is None else ArtifactIntegrity.VERIFIED,
                          owned=artifact.request.job_id == job_id and artifact.request.project_id == document.project_id,
                          schema_valid=schema_valid, expected=tuple(reuse_dimensions(document, self._plan).items()),
                          produced=produced),
            # The output is always built again from the replayed raw results.
            StageEvidence(ResumeStage.RECONSTRUCT, ArtifactIntegrity.MISSING),
        )
        selection = select_resume_stage(evidence)
        if selection.resume_from is not ResumeStage.RECONSTRUCT:
            run.emit(JobWarning(f"raw_results_{selection.reason.value}"))
            for name in selection.changed:
                run.emit(JobWarning(f"raw_results_changed_{name}"))
            return None
        try:
            raw = self._decode_raw(payload, document, run.cache_scope)
        except TranslationExecutionError:
            # Every recorded dimension matched, so something unrecorded differs. Prove nothing and redo.
            run.emit(JobWarning("raw_results_provenance_unproven"))
            return None
        results = []
        for index, unit in enumerate(document.units):
            run.checkpoint("pipeline_before_unit")
            results.append(run.apply_raw(unit, raw[index]))
            run.progress(index + 1)
        self._record_replay(run, artifact)
        return run.result(tuple(results), artifact.artifact_id)
    return None
