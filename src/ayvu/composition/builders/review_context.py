"""Resolve review documents and verify reconstructed machine bytes without publication."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from collections.abc import Iterable

from ...artifacts import Artifact, ArtifactCancelledError, ArtifactIntegrityError, ArtifactRequest, ArtifactState
from ...content import ContentDocument
from ...formats import AliasVerdict, ContentScope, ExtractionRequest, ReconstructionRequest
from ...formats import FormatPublication, FormatSource
from ...jobs import JobState
from ...projects import ProjectManifest
from ...review import ReviewApplicationError, ReviewPackageIdentity
from ...review import ReviewExchangeError, ReviewPackage, ReviewReconstructionPlan, map_review_package
from ...translation import OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE
from ...kernel import CancellationToken

from .review_files import _safe_code


_EPUB_MEDIA_TYPE = "application/epub+zip"


class _DigestOnlyPublisher:
    """Consumes reconstructed machine bytes in memory without creating or publishing a payload."""

    def __init__(self) -> None:
        self.size = 0
        self.digest: str | None = None

    def __repr__(self) -> str:
        return "DigestOnlyPublisher()"

    def publish(self, request: ArtifactRequest, chunks: Iterable[bytes], *, owner_token: str,
                cancellation: CancellationToken | None = None) -> Artifact:
        hasher = hashlib.sha256()
        for chunk in chunks:
            if cancellation is not None and cancellation.checkpoint("artifact_between_chunks") is not None:
                raise ArtifactCancelledError("cancelled")
            if type(chunk) is not bytes:
                raise ArtifactIntegrityError("invalid_chunk")
            self.size += len(chunk)
            hasher.update(chunk)
        self.digest = f"sha256:{hasher.hexdigest()}"
        stable_time = datetime(2000, 1, 1, tzinfo=UTC)
        return Artifact("00000000-0000-4000-8000-000000000000", request, ArtifactState.READY, 1,
                        stable_time, stable_time, self.size, self.digest)


class _DistinctGuard:
    def __repr__(self) -> str:
        return "DistinctGuard()"

    @staticmethod
    def verdict(source: FormatSource) -> AliasVerdict:
        return AliasVerdict.DISTINCT


@dataclass(frozen=True, repr=False)
class _MappedReview:
    project: ProjectManifest
    identity: ReviewPackageIdentity
    package: ReviewPackage
    machine_artifact: Artifact
    source: FormatSource
    documents: tuple[ContentDocument, ...]
    plan: ReviewReconstructionPlan
    format_version: str

    def __repr__(self) -> str:
        return f"MappedReview(documents={len(self.documents)}, units={len(self.plan.translations)})"


def _map_review_context(self, package: ReviewPackage, identity: ReviewPackageIdentity, source: object | None,
                 cancellation: CancellationToken | None) -> _MappedReview:
    project = self._load_project(identity.project_id or "")
    actual_source = self._source(project, source)
    if (project.revision != identity.project_revision or project.source_sha256 != identity.source_sha256
            or project.source_size != identity.source_size):
        raise ReviewApplicationError("stale_project_revision")
    if cancellation is not None and cancellation.checkpoint("review_before_validation") is not None:
        raise ReviewApplicationError("cancelled")
    inspection = self._formats.inspect("epub", actual_source, cancellation=cancellation)
    if inspection.failure is not None or inspection.inventory is None or inspection.inventory.format_version is None:
        raise ReviewApplicationError("incompatible_format")
    machine_artifact, machine_job = _resolve_machine_context(self, identity, project)
    documents = _extract_package_documents(self._formats, actual_source, project, package, cancellation)
    machine_translations = self._machine_translations(package)
    plan = _map_review_plan(self._formats.descriptor("epub"), package, project, machine_artifact,
                            inspection.inventory.format_version,
                            documents, machine_translations)
    _verify_machine_digest(self._formats, actual_source, package, project, identity, machine_artifact, machine_job,
                           inspection.inventory.format_version, documents, machine_translations, cancellation)
    self._verify_machine_provenance(project.project_id, machine_job.job_id, machine_artifact,
                                    (row.unit_id for row in package.rows))
    return _MappedReview(project, identity, package, machine_artifact, actual_source,
                         plan.documents, plan, inspection.inventory.format_version)


def _resolve_machine_context(self, identity, project):
    artifact_service = self._read_only_artifacts(project.project_id)
    try:
        machine_artifact = artifact_service.get(identity.machine_artifact_id or "")
    except Exception as error:
        raise ReviewApplicationError(_safe_code(error, "machine_artifact_unavailable")) from None
    if (not self._is_machine_artifact(machine_artifact)
            or machine_artifact.revision != identity.machine_artifact_revision
            or machine_artifact.digest != identity.machine_artifact_digest):
        raise ReviewApplicationError("machine_artifact_not_ready")
    jobs = self._read_only_jobs(project.project_id)
    try:
        machine_job = jobs.get(machine_artifact.request.job_id).job
    except Exception as error:
        raise ReviewApplicationError(_safe_code(error, "machine_job_unavailable")) from None
    if (machine_job.request.project_id != project.project_id
            or machine_job.request.command_type != TRANSLATE_COMMAND_TYPE
            or machine_job.state not in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)
            or OUTPUT_ROLE not in machine_job.request.output_roles):
        raise ReviewApplicationError("machine_job_unavailable")
    return machine_artifact, machine_job


def _extract_package_documents(formats, actual_source, project, package, cancellation):
    package_documents = {row.document_ref for row in package.rows}
    document_revisions: dict[str, set[str]] = {}
    for row in package.rows:
        document_revisions.setdefault(row.document_ref, set()).add(row.document_revision_id)
    scope_options = (
        frozenset((ContentScope.BODY,)),
        frozenset((ContentScope.BODY, ContentScope.METADATA, ContentScope.NAVIGATION)),
        frozenset((ContentScope.BODY, ContentScope.IMAGE_ALT_TEXT)),
        frozenset((ContentScope.BODY, ContentScope.METADATA, ContentScope.NAVIGATION, ContentScope.IMAGE_ALT_TEXT)),
    )
    matched: dict[tuple[tuple[str, str], ...], tuple[ContentDocument, ...]] = {}
    observed_document_ids: set[str] = set()
    for scopes in scope_options:
        if cancellation is not None and cancellation.checkpoint("review_before_validation") is not None:
            raise ReviewApplicationError("cancelled")
        extracted = formats.extract("epub", actual_source,
                                          ExtractionRequest(project.project_id, scopes), cancellation=cancellation)
        if extracted.failure is not None or extracted.documents is None:
            continue
        observed_document_ids.update(item.document_id for item in extracted.documents)
        by_id = {item.document_id: item for item in extracted.documents}
        if not package_documents <= by_id.keys():
            continue
        documents = tuple(by_id[document_id] for document_id in sorted(package_documents))
        if any(document_revisions[document.document_id] != {document.revision_id} for document in documents):
            continue
        signature = tuple((document.document_id, document.revision_id) for document in documents)
        matched[signature] = documents
    if not matched:
        code = "unknown_document" if package_documents - observed_document_ids else "stale_document_revision"
        raise ReviewApplicationError(code)
    documents = next(iter(matched.values()))
    return documents


def _map_review_plan(descriptor, package, project, machine_artifact, format_version, documents, machine_translations):
    try:
        plan = map_review_package(
            package, project=project, machine_artifact=machine_artifact,
            descriptor=descriptor,
            format_version=format_version,
            documents=documents, machine_translations=machine_translations,
        )
    except ReviewExchangeError as error:
        raise ReviewApplicationError(str(error)) from None
    return plan


def _verify_machine_digest(formats, actual_source, package, project, identity, machine_artifact, machine_job,
                           format_version, documents, machine_translations, cancellation):
    # The artifact payload is a user-selected destination and is intentionally not retained as project data.
    # Reconstruct its machine bytes from the immutable package rows and compare the exact D106 digest instead.
    machine_package = replace(package, rows=tuple(replace(row, reviewed_text=row.machine_translation) for row in package.rows))
    try:
        machine_plan = map_review_package(
            machine_package, project=project, machine_artifact=machine_artifact,
            descriptor=formats.descriptor("epub"), format_version=format_version,
            documents=documents, machine_translations=machine_translations)
    except ReviewExchangeError as error:
        raise ReviewApplicationError(str(error)) from None
    request = ArtifactRequest(project.project_id, machine_job.job_id, "review_machine_check", "machine_digest", 1,
                              identity.package_fingerprint, _EPUB_MEDIA_TYPE)
    publisher = _DigestOnlyPublisher()
    materialized = formats.reconstruct(
        "epub", actual_source, ReconstructionRequest(machine_plan.documents, machine_plan.translations),
        FormatPublication(publisher, request, "review_validation", _DistinctGuard()), cancellation=cancellation)
    if (materialized.failure is not None or materialized.artifact is None
            or materialized.artifact.digest != identity.machine_artifact_digest):
        raise ReviewApplicationError("machine_artifact_mismatch")
