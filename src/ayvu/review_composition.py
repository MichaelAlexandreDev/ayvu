"""Concrete review adapters: v2 codec, local project stores, EPUB format work and D106 publication."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Iterable
from typing import Callable
from uuid import uuid4

from .artifact import (Artifact, ArtifactCancelledError, ArtifactConflictError, ArtifactError, ArtifactIntegrityError,
                       ArtifactRequest, ArtifactState)
from .artifact_destination import DestinationPublications, LocalDestinationPayloads
from .artifact_service import ArtifactService
from .artifact_store import LocalArtifactPayloads, SqliteArtifactRepository
from .content import ContentDocument
from .epub_format import EPUB_ADAPTER, epub_format_adapter
from .format_adapter import AliasVerdict, ContentScope, ExtractionRequest, ReconstructionRequest
from .format_local import LocalFormatSource, LocalOutputGuard
from .format_service import FormatPublication, FormatService, FormatRegistry, FormatSource
from .html_structure import html_unit_template
from .job import JobRequest, JobState, JobTerminalSummary, MAX_LEASE_DURATION
from .job_events import (
    CancellationAcknowledgement, CancellationRequest, JobCorrelation, JobEvent, JobFailure, JobFailureFamily, JobOutcome,
    JobProgress, JobWarning,
)
from .job_execution import CancellationSource, CancellationToken, InMemoryJobEventLog
from .job_service import JobRepository
from .job_store import SqliteJobRepository
from .project import ProjectManifest
from .project_schema import (
    open_artifact_service, open_job_repository, open_provenance_repository, project_migration_runner,
)
from .project_store import ProjectJsonRepository, file_digest
from .provenance import (
    UNKNOWN, ActorKind, ProducerIdentity, ProvenanceError, ProvenanceRequest, SourceKind, SubjectRef,
)
from .provenance_store import SqliteProvenanceRepository
from .review_application import (
    REVIEW_APPLY_COMMAND_TYPE, REVIEW_EXPORT_COMMAND_TYPE, REVIEW_SAFE_POINTS, ReviewApplicationError,
    ReviewCounts, ReviewOperation, ReviewOperationResult, ReviewPackageExport, ReviewPackageIdentity,
    ReviewUseCasePorts, ReviewValidationMaterial, ExportReviewPackage, ValidateReviewPackage, ApplyReviewPackage,
)
from .review_exchange import (
    REVIEW_PACKAGE_SCHEMA_VERSION, ReviewExchangeError, ReviewPackage, ReviewReconstructionPlan,
    decode_review_package, encode_review_package, export_review_package, map_review_package,
)
from .review_import import ReviewImportError, read_review_csv_bytes
from .translate_project import TranslateProjectResult
from .translate_project import OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, TRANSLATE_COMMAND_TYPE
from .translation_engine import STATUS_CACHE, STATUS_ERROR, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED

_MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_MAX_V1_BYTES = 32 * 1024 * 1024
_REVIEW_EXPORT_ROLE = "review_package"
_REVIEW_EXPORT_STAGE = "review_export"
_REVIEW_APPLY_ROLE = "reviewed_output"
_REVIEW_APPLY_STAGE = "review_reconstruct"
_EPUB_MEDIA_TYPE = "application/epub+zip"
_CSV_MEDIA_TYPE = "text/csv"
_UNKNOWN_PRODUCER = ProducerIdentity(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)
_MACHINE_LINEAGE_KINDS = frozenset((SourceKind.PROVIDER, SourceKind.CACHE, SourceKind.TRANSLATION_MEMORY, SourceKind.PIPELINE))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _lineage_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((kind, *parts)).encode("utf-8")).hexdigest()
    return f"review-{kind}:{digest}"


def _operation_subject(idempotency_key: str) -> SubjectRef:
    return SubjectRef("review_operation", 1, _fingerprint(idempotency_key.encode("utf-8")))


def _destination_subject(destination: object) -> SubjectRef:
    canonical = str(Path(destination).expanduser().resolve(strict=False))
    return SubjectRef("artifact_destination", 1, _fingerprint(canonical.encode("utf-8")))


def _safe_code(error: Exception, fallback: str) -> str:
    code = str(error)
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else fallback


def read_review_input(path: Path | str) -> bytes:
    """Read one bounded regular package file without following links or echoing its path."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(Path(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReviewApplicationError("unsafe_review_file")
            if info.st_size > _MAX_PACKAGE_BYTES:
                raise ReviewApplicationError("review_file_too_large")
            raw = stream.read(_MAX_PACKAGE_BYTES + 1)
    except ReviewApplicationError:
        raise
    except FileNotFoundError:
        raise ReviewApplicationError("review_file_not_found") from None
    except OSError:
        raise ReviewApplicationError("review_file_unreadable") from None
    if len(raw) > _MAX_PACKAGE_BYTES:
        raise ReviewApplicationError("review_file_too_large")
    if len(raw) > _MAX_V1_BYTES and not raw.startswith(b"AYVU-REVIEW-PACKAGE"):
        raise ReviewApplicationError("review_file_too_large")
    return raw


def destination_fingerprint(path: Path | str) -> str:
    """Stable output target identity that can travel in commands without exposing the path."""
    canonical = str(Path(path).expanduser().resolve(strict=False))
    return _fingerprint(canonical.encode("utf-8"))


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


class _ReviewRunGuard:
    """Serializes a request key and its destination without conflating their identities."""

    def __init__(self, guard: object, identities: tuple[str, ...]) -> None:
        self._guard = guard
        self._identities = tuple(sorted(set(identities)))
        self._entered: list[object] = []

    def __repr__(self) -> str:
        return "ReviewRunGuard()"

    def __enter__(self) -> bool:
        try:
            for identity in self._identities:
                context = self._guard.exclusive(identity)
                alone = context.__enter__()
                self._entered.append(context)
                if not alone:
                    self.__exit__(None, None, None)
                    return False
            return True
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, error_type: object, error: object, traceback: object) -> bool:
        entered, self._entered = self._entered, []
        for context in reversed(entered):
            context.__exit__(error_type, error, traceback)
        return False


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


class LocalReviewPackagePort:
    """Read-only context resolution plus the existing DESKTOP-117 codec and mapper."""

    def __init__(self, processing_dir: Path, formats: FormatService, *, source: FormatSource | None = None) -> None:
        self._processing_dir = Path(processing_dir)
        self._formats = formats
        self._source_hint = source
        self._projects = ProjectJsonRepository(self._processing_dir)

    def __repr__(self) -> str:
        return "LocalReviewPackagePort()"

    @staticmethod
    def destination_fingerprint(destination: object) -> str:
        try:
            return destination_fingerprint(Path(destination))
        except Exception:
            raise ReviewApplicationError("invalid_destination") from None

    def export(self, translation: TranslateProjectResult, destination: object) -> ReviewPackageExport:
        if (type(translation) is not TranslateProjectResult or not translation.output_ready
                or translation.job_id is None or translation.artifact is None or translation.project_revision is None
                or translation.state not in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)
                or not translation.engine.documents or not translation.engine.translations
                or translation.artifact_destination is None):
            raise ReviewApplicationError("translation_identity_unavailable")
        project = self._load_project(translation.project_id)
        if project.revision != translation.project_revision:
            raise ReviewApplicationError("stale_project_revision")
        if (not translation.engine.documents[0].source_fingerprint
                or project.source_sha256 != translation.engine.documents[0].source_fingerprint.removeprefix("sha256:")):
            raise ReviewApplicationError("source_mismatch")
        artifacts = self._read_only_artifacts(project.project_id)
        machine_artifact = artifacts.get(translation.artifact.artifact_id)
        if (machine_artifact != translation.artifact or machine_artifact.request.job_id != translation.job_id
                or not self._is_machine_artifact(machine_artifact)):
            raise ReviewApplicationError("machine_artifact_mismatch")
        try:
            output_source = LocalFormatSource(Path(translation.artifact_destination))
            if (output_source.size(), output_source.fingerprint()) != (machine_artifact.size, machine_artifact.digest):
                raise ReviewApplicationError("machine_artifact_changed")
        except ReviewApplicationError:
            raise
        except Exception:
            raise ReviewApplicationError("machine_artifact_unavailable") from None
        jobs = self._read_only_jobs(project.project_id)
        job = jobs.get(translation.job_id).job
        if (job.request.project_id != project.project_id or not job.terminal
                or job.request.command_type != TRANSLATE_COMMAND_TYPE
                or job.state not in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)
                or OUTPUT_ROLE not in job.request.output_roles):
            raise ReviewApplicationError("translation_job_unavailable")
        expected_unit_ids = tuple(unit.unit_id for document in translation.engine.documents for unit in document.units)
        translation_ids = tuple(item.unit_id for item in translation.engine.translations)
        unit_statuses = dict(translation.engine.unit_sources)
        if (len(set(expected_unit_ids)) != len(expected_unit_ids)
                or len(set(translation_ids)) != len(translation_ids)
                or set(translation_ids) != set(expected_unit_ids)
                or len(unit_statuses) != len(translation.engine.unit_sources)
                or not set(expected_unit_ids) <= set(unit_statuses)):
            raise ReviewApplicationError("translation_unit_identity_unavailable")
        self._verify_machine_provenance(project.project_id, job.job_id, machine_artifact,
                                        expected_unit_ids, unit_statuses)
        source = self._source(project)
        destination_guard = LocalOutputGuard(Path(destination))
        if destination_guard.verdict(source) is not AliasVerdict.DISTINCT:
            raise ReviewApplicationError("review_output_alias_source")
        if destination_guard.verdict(output_source) is not AliasVerdict.DISTINCT:
            raise ReviewApplicationError("review_output_alias_machine_artifact")
        inspected = self._formats.inspect("epub", source)
        if inspected.failure is not None or inspected.inventory is None or inspected.inventory.format_version is None:
            raise ReviewApplicationError("incompatible_format")
        machine_translations = {item.unit_id: item.text for item in translation.engine.translations}
        package = export_review_package(
            project=project,
            machine_artifact=machine_artifact,
            descriptor=epub_format_adapter().descriptor,
            format_version=inspected.inventory.format_version,
            documents=translation.engine.documents,
            machine_translations=machine_translations,
        )
        raw = encode_review_package(package)
        identity = self._identity(package, raw)
        unit_ids = tuple(row.unit_id for row in package.rows)
        return ReviewPackageExport(identity, raw, unit_ids)

    def inspect(self, raw: bytes) -> ReviewPackageIdentity:
        if type(raw) is not bytes or len(raw) > _MAX_PACKAGE_BYTES:
            raise ReviewApplicationError("review_file_too_large" if isinstance(raw, bytes) else "invalid_package")
        try:
            package = decode_review_package(raw)
        except ReviewExchangeError as v2_error:
            try:
                legacy = read_review_csv_bytes(raw)
            except ReviewImportError:
                code = str(v2_error)
                raise ReviewApplicationError(code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "invalid_review_package") from None
            document_count = len({row.document_path for row in legacy.rows if row.document_path})
            return ReviewPackageIdentity(1, None, _fingerprint(raw), document_count=document_count,
                                         unit_count=len(legacy.rows))
        return self._identity(package, raw)

    def validate(self, raw: bytes, identity: ReviewPackageIdentity, source: object | None,
                 cancellation: CancellationToken | None = None) -> ReviewValidationMaterial:
        try:
            package = decode_review_package(raw)
        except ReviewExchangeError as error:
            raise ReviewApplicationError(str(error)) from None
        current_identity = self._identity(package, raw)
        if current_identity != identity:
            raise ReviewApplicationError("package_changed")
        if identity.schema_version != REVIEW_PACKAGE_SCHEMA_VERSION:
            raise ReviewApplicationError("legacy_reexport_required")
        try:
            context = self._map_context(package, identity, source, cancellation)
        except ReviewApplicationError as error:
            counts = ReviewCounts(len({row.document_ref for row in package.rows}), len(package.rows),
                                  sum(row.review_status == "reviewed" for row in package.rows),
                                  sum(row.review_status != "reviewed" for row in package.rows))
            return ReviewValidationMaterial(identity, False, counts, (error.code,))
        reviewed = tuple(row.unit_id for row in package.rows if row.review_status == "reviewed")
        counts = ReviewCounts(identity.document_count, identity.unit_count, len(reviewed), identity.unit_count - len(reviewed))
        return ReviewValidationMaterial(identity, True, counts, plan=context, reviewed_unit_ids=reviewed)

    @staticmethod
    def _identity(package: ReviewPackage, raw: bytes) -> ReviewPackageIdentity:
        docs = len({row.document_ref for row in package.rows})
        return ReviewPackageIdentity(
            schema_version=package.header.schema_version,
            package_id=package.header.package_id,
            package_fingerprint=_fingerprint(raw),
            project_id=package.header.project_id,
            project_revision=package.header.project_revision,
            source_sha256=package.header.source_sha256,
            source_size=package.header.source_size,
            machine_artifact_id=package.header.machine_artifact_id,
            machine_artifact_revision=package.header.machine_artifact_revision,
            machine_artifact_digest=package.header.machine_artifact_digest,
            document_count=docs,
            unit_count=len(package.rows),
        )

    @staticmethod
    def _is_machine_artifact(artifact: Artifact) -> bool:
        return (artifact.state is ArtifactState.READY and artifact.request.role == OUTPUT_ROLE
                and artifact.request.stage == RECONSTRUCT_STAGE
                and artifact.request.stage_version == RECONSTRUCT_STAGE_VERSION
                and artifact.request.media_type == _EPUB_MEDIA_TYPE)

    def _load_project(self, project_id: str) -> ProjectManifest:
        try:
            return self._projects.load_read_only(project_id)
        except Exception as error:
            raise ReviewApplicationError(_safe_code(error, "project_unavailable")) from None

    def _source(self, project: ProjectManifest, source: object | None = None) -> FormatSource:
        candidate = source if source is not None else self._source_hint
        if candidate is None:
            candidate = LocalFormatSource(project.source_path)
        try:
            actual_size = candidate.size()
            actual_fingerprint = candidate.fingerprint()
        except Exception:
            raise ReviewApplicationError("source_unreadable") from None
        if (actual_size, actual_fingerprint) != (project.source_size, f"sha256:{project.source_sha256}"):
            raise ReviewApplicationError("source_changed")
        return candidate

    def _read_only_artifacts(self, project_id: str) -> ArtifactService:
        try:
            paths = self._projects.read_database_paths(project_id)
            root = self._projects.read_artifact_root(project_id)
            runner = project_migration_runner()
            repository = SqliteArtifactRepository(runner, paths.database, project_id)
            return ArtifactService(repository, LocalArtifactPayloads(root))
        except Exception as error:
            raise ReviewApplicationError(_safe_code(error, "project_storage_unavailable")) from None

    def _read_only_jobs(self, project_id: str) -> JobRepository:
        try:
            paths = self._projects.read_database_paths(project_id)
            return SqliteJobRepository(project_migration_runner(), paths.database, project_id)
        except Exception as error:
            raise ReviewApplicationError(_safe_code(error, "project_storage_unavailable")) from None

    def _read_only_provenance(self, project_id: str) -> SqliteProvenanceRepository:
        try:
            paths = self._projects.read_database_paths(project_id)
            return SqliteProvenanceRepository(project_migration_runner(), paths.database, project_id)
        except Exception as error:
            raise ReviewApplicationError(_safe_code(error, "project_storage_unavailable")) from None

    def _verify_machine_provenance(self, project_id: str, job_id: str, artifact: Artifact,
                                   unit_ids: object, statuses: dict[str, str] | None = None) -> None:
        provenance = self._read_only_provenance(project_id)
        seen: set[str] = set()
        expected = {
            STATUS_CACHE: (SourceKind.CACHE, "machine_translation"),
            STATUS_MEMORY: (SourceKind.TRANSLATION_MEMORY, "machine_translation"),
            STATUS_TRANSLATED: (SourceKind.PROVIDER, "machine_translation"),
            STATUS_MISSING: (SourceKind.PIPELINE, "translation_missing"),
            STATUS_ERROR: (SourceKind.PIPELINE, "translation_failed"),
            "replay": (SourceKind.PIPELINE, "machine_translation"),
        }
        for unit_id in unit_ids:
            if unit_id in seen:
                raise ReviewApplicationError("duplicate_translation_unit")
            seen.add(unit_id)
            try:
                graph = provenance.list_by_subject(SubjectRef("translation_unit", 1, unit_id), limit=64)
            except ProvenanceError as error:
                raise ReviewApplicationError(_safe_code(error, "unit_provenance_unavailable")) from None
            if graph.truncated:
                raise ReviewApplicationError("unit_provenance_history_incomplete")
            status = None if statuses is None else statuses.get(unit_id)
            expected_record = expected.get(status) if status is not None else None
            if (status is not None and expected_record is None):
                raise ReviewApplicationError("translation_unit_identity_unavailable")
            if not any(node.record.request.job_id == job_id
                       and node.record.request.operation == "translate_unit"
                       and node.record.request.source_kind in _MACHINE_LINEAGE_KINDS
                       and node.record.request.outcome_code in {"machine_translation", "translation_missing", "translation_failed"}
                       and (expected_record is None or (node.record.request.source_kind, node.record.request.outcome_code) == expected_record)
                       and artifact.artifact_id in node.record.request.artifacts
                       for node in graph.nodes):
                raise ReviewApplicationError("unit_provenance_unavailable")

    def _map_context(self, package: ReviewPackage, identity: ReviewPackageIdentity, source: object | None,
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
            extracted = self._formats.extract("epub", actual_source,
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
        machine_translations = self._machine_translations(package)
        try:
            plan = map_review_package(
                package, project=project, machine_artifact=machine_artifact,
                descriptor=epub_format_adapter().descriptor,
                format_version=inspection.inventory.format_version,
                documents=documents, machine_translations=machine_translations,
            )
        except ReviewExchangeError as error:
            raise ReviewApplicationError(str(error)) from None
        # The artifact payload is a user-selected destination and is intentionally not retained as project data.
        # Reconstruct its machine bytes from the immutable package rows and compare the exact D106 digest instead.
        machine_package = replace(package, rows=tuple(replace(row, reviewed_text=row.machine_translation) for row in package.rows))
        try:
            machine_plan = map_review_package(
                machine_package, project=project, machine_artifact=machine_artifact,
                descriptor=epub_format_adapter().descriptor, format_version=inspection.inventory.format_version,
                documents=documents, machine_translations=machine_translations)
        except ReviewExchangeError as error:
            raise ReviewApplicationError(str(error)) from None
        request = ArtifactRequest(project.project_id, machine_job.job_id, "review_machine_check", "machine_digest", 1,
                                  identity.package_fingerprint, _EPUB_MEDIA_TYPE)
        publisher = _DigestOnlyPublisher()
        materialized = self._formats.reconstruct(
            "epub", actual_source, ReconstructionRequest(machine_plan.documents, machine_plan.translations),
            FormatPublication(publisher, request, "review_validation", _DistinctGuard()), cancellation=cancellation)
        if (materialized.failure is not None or materialized.artifact is None
                or materialized.artifact.digest != identity.machine_artifact_digest):
            raise ReviewApplicationError("machine_artifact_mismatch")
        self._verify_machine_provenance(project.project_id, machine_job.job_id, machine_artifact,
                                        (row.unit_id for row in package.rows))
        return _MappedReview(project, identity, package, machine_artifact, actual_source,
                             plan.documents, plan, inspection.inventory.format_version)

    @staticmethod
    def _machine_translations(package: ReviewPackage) -> dict[str, str]:
        translations: dict[str, str] = {}
        for row in package.rows:
            text = row.machine_translation
            for index, (placeholder, _kind) in enumerate(row.protected_parts):
                text = text.replace(placeholder, f"__AYVU_TAG_{index}__")
            translations[row.unit_id] = text
        return translations


@dataclass(repr=False)
class _OperationState:
    project: ProjectManifest
    jobs: JobRepository
    artifacts: ArtifactService
    provenance: SqliteProvenanceRepository
    publications: DestinationPublications
    request: ArtifactRequest
    job_id: str
    owner_token: str
    job: object
    operation_id: str
    idempotency_key: str
    command_type: str
    guard: object
    guard_open: bool
    events: InMemoryJobEventLog
    event_sequence: int
    clock: Callable[[], datetime]
    new_id: Callable[[], str]
    format_service: FormatService
    terminal: ReviewOperationResult | None = None
    published_artifact: Artifact | None = None


def _has_recorded_terminal_outcome(state: _OperationState, expected_code: str) -> bool:
    outcome = state.events.outcome
    return (state.job.state is JobState.VALIDATED and outcome is not None
            and outcome.state is JobState.VALIDATED and outcome.summary.reason_code == expected_code)


class LocalReviewOperations:
    """Concrete job/artifact/provenance owner. Files publish through D106's journal and reconciliation."""

    def __init__(self, processing_dir: Path, formats: FormatService, *, clock: Callable[[], datetime] = _utc_now,
                 new_id: Callable[[], str] = lambda: str(uuid4()), lease_duration: timedelta = MAX_LEASE_DURATION) -> None:
        self._processing_dir = Path(processing_dir)
        self._projects = ProjectJsonRepository(self._processing_dir)
        self._formats = formats
        self._clock = clock
        self._new_id = new_id
        self._lease_duration = lease_duration

    def __repr__(self) -> str:
        return "LocalReviewOperations()"

    def cancellation(self) -> CancellationSource:
        return CancellationSource(tuple(dict.fromkeys((*REVIEW_SAFE_POINTS, *self._formats.safe_points("epub")))))

    def begin(self, project_id: str, command_type: str, input_fingerprint: str, idempotency_key: str,
              destination: object, overwrite: bool) -> ReviewOperation:
        if command_type not in (REVIEW_EXPORT_COMMAND_TYPE, REVIEW_APPLY_COMMAND_TYPE):
            raise ReviewApplicationError("invalid_operation")
        project = self._load_project(project_id)
        paths = self._projects.resolve_database_paths(project_id)
        artifact_root = self._projects.resolve_artifact_root(project_id)
        jobs = open_job_repository(paths.database, paths.backups, project_id, clock=self._clock, new_id=self._new_id)
        payloads = LocalDestinationPayloads(artifact_root, destination=Path(destination), replace_existing=overwrite)
        artifacts = open_artifact_service(paths.database, paths.backups, artifact_root, project_id,
                                          payloads=payloads, clock=self._clock, new_id=self._new_id)
        provenance = open_provenance_repository(paths.database, paths.backups, project_id,
                                                clock=self._clock, new_id=self._new_id)
        role, stage, media = self._labels(command_type)
        request = None
        state: _OperationState | None = None
        created_job = None
        guard_entered = False
        target_fingerprint = destination_fingerprint(destination)
        guard = _ReviewRunGuard(self._projects.run_guard(project_id),
                                ("idempotency:" + _fingerprint(idempotency_key.encode("utf-8")),
                                 "destination:" + target_fingerprint))
        try:
            alone = guard.__enter__()
            guard_entered = True
            if not alone:
                raise ReviewApplicationError("job_in_progress")
            # Reconcile D106-owned rename records after the previous owner is gone. Active jobs stay untouched.
            artifacts.reconcile()
            if command_type == REVIEW_APPLY_COMMAND_TYPE:
                try:
                    destinations = provenance.list_by_subject(_destination_subject(destination), limit=64)
                except ProvenanceError as error:
                    raise ReviewApplicationError(_safe_code(error, "destination_history_unavailable")) from None
                if destinations.truncated:
                    raise ReviewApplicationError("destination_history_incomplete")
                if any(node.record.request.operation == "translate_output"
                       and node.record.request.artifacts
                       for node in destinations.nodes):
                    raise ReviewApplicationError("output_is_machine_artifact")
            request = JobRequest(project_id, command_type, 1, input_fingerprint, (role,))
            try:
                history = provenance.list_by_subject(_operation_subject(idempotency_key), limit=1024)
            except ProvenanceError as error:
                raise ReviewApplicationError(_safe_code(error, "idempotency_history_unavailable")) from None
            if history.truncated:
                raise ReviewApplicationError("idempotency_history_incomplete")
            key_registered = False
            for node in history.nodes:
                record = node.record
                key_registered = True
                prior = jobs.get(record.request.job_id).job
                if (prior.request.command_type != command_type
                        or prior.request.input_fingerprint != input_fingerprint):
                    raise ReviewApplicationError("idempotency_conflict")
                committed_outcome = "review_exported" if command_type == REVIEW_EXPORT_COMMAND_TYPE else "review_applied"
                if record.request.outcome_code != committed_outcome:
                    continue
                prior_request = ArtifactRequest(project_id, prior.job_id, role, stage, 1, input_fingerprint, media)
                ready = artifacts.find_reusable(prior_request).artifact
                if ready is None:
                    # The person removed or changed that file, so it can no longer be replayed. This attempt
                    # is new work, and an existing destination still goes through the overwrite policy.
                    continue
                settled = prior if prior.state is JobState.VALIDATED else self._reconcile_success(jobs, prior)
                if settled.state is JobState.VALIDATED:
                    guard.__exit__(None, None, None)
                    return ReviewOperation(project_id, prior.job_id, input_fingerprint, idempotency_key,
                                           token=None, already_committed=True, artifact=ready)
            # A prior fully committed request is an idempotent replay, independent of CLI overwrite policy.
            for snapshot in jobs.list_jobs(project_id, limit=256, newest_first=True):
                prior = snapshot.job
                if prior.request.command_type != command_type:
                    continue
                if prior.request.input_fingerprint != input_fingerprint:
                    continue
                prior_request = ArtifactRequest(project_id, prior.job_id, role, stage, 1, input_fingerprint, media)
                if not prior.terminal:
                    # The project run guard proves no live owner remains. Resolve its artifact before deciding
                    # whether a prior rename can be reported; a missing human lineage never becomes success.
                    try:
                        current = jobs.get(prior.job_id).job
                        if current.lease is not None:
                            jobs.release_lease(current.job_id, current.lease.owner_token)
                            current = jobs.get(current.job_id).job
                        artifacts.reconcile()
                        stale_artifact = artifacts.find_ready(prior_request)
                        target = JobState.PARTIALLY_COMPLETED if stale_artifact is not None else JobState.FAILED
                        reason = "publication_uncommitted" if stale_artifact is not None else "interrupted"
                        self._terminalize(jobs, current, target, reason)
                    except Exception:
                        raise ReviewApplicationError("job_reconciliation_failed") from None
            job = jobs.create(request)
            created_job = job
            job = jobs.acquire_lease(job.job_id, self._lease_duration)
            owner_token = job.lease.owner_token
            job = jobs.transition(job.job_id, JobState.PREFLIGHT, expected_revision=job.revision)
            job = jobs.transition(job.job_id, JobState.RUNNING, expected_revision=job.revision)
            publications = DestinationPublications(artifacts, payloads, Path(destination))
            operation_id = self._new_id()
            if not key_registered:
                operation = "review_export" if command_type == REVIEW_EXPORT_COMMAND_TYPE else "apply_review"
                provenance.append(ProvenanceRequest(
                    project_id, job.job_id, operation, ActorKind.SYSTEM, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
                    subject=_operation_subject(idempotency_key), outcome_code="review_started",
                    operation_id=operation_id, idempotency_key=idempotency_key))
            event_log = InMemoryJobEventLog(job.job_id)
            state = _OperationState(project, jobs, artifacts, provenance, publications,
                                    ArtifactRequest(project_id, job.job_id, role, stage, 1, input_fingerprint, media),
                                    job.job_id, owner_token, job, operation_id, idempotency_key, command_type,
                                    guard, True, event_log, 0, self._clock, self._new_id, self._formats)
            self._emit(state, JobProgress("preflight", "step", 1, 1))
            return ReviewOperation(project_id, job.job_id, input_fingerprint, idempotency_key, state)
        except ReviewApplicationError:
            if state is None and guard_entered:
                if created_job is not None:
                    self._settle_unstarted_job(jobs, created_job)
                guard.__exit__(None, None, None)
            raise
        except Exception as error:
            if state is None and guard_entered:
                if created_job is not None:
                    self._settle_unstarted_job(jobs, created_job)
                guard.__exit__(None, None, None)
            raise ReviewApplicationError(_safe_code(error, "review_operation_unavailable")) from None

    def publish_package(self, operation: ReviewOperation, package: bytes, identity: ReviewPackageIdentity,
                        destination: object,
                        cancellation: CancellationToken | None = None) -> Artifact:
        state = self._state(operation)
        try:
            def validate_staged(staged: object) -> None:
                actual = read_review_input(Path(staged))
                if actual != package:
                    raise ArtifactIntegrityError("review_package_changed")
                try:
                    decoded = decode_review_package(actual)
                    measured = LocalReviewPackagePort._identity(decoded, actual)
                except ReviewExchangeError:
                    raise ArtifactIntegrityError("invalid_review_package") from None
                if measured != identity:
                    raise ArtifactIntegrityError("review_package_changed")

            state.publications.publication(state.request, state.owner_token, validate_staged)
            self._emit(state, JobProgress("publishing", "step", 0, 1))
            artifact = state.publications.publish(state.request, (package,), owner_token=state.owner_token,
                                                  cancellation=cancellation)
            state.published_artifact = artifact
            self._emit(state, JobProgress("publishing", "step", 1, 1))
            return artifact
        except ArtifactError as error:
            raise ReviewApplicationError(_safe_code(error, "publication_failed")) from None

    def reconstruct(self, operation: ReviewOperation, validation: ReviewValidationMaterial, source: object,
                    destination: object, overwrite: bool,
                    cancellation: CancellationToken | None = None) -> Artifact:
        state = self._state(operation)
        mapped = validation.plan
        if type(mapped) is not _MappedReview or not validation.valid:
            raise ReviewApplicationError("invalid_review_plan")
        source = mapped.source if source is None else source
        output_path = Path(destination)
        source_guard = LocalOutputGuard(output_path)
        if source_guard.verdict(source) is not AliasVerdict.DISTINCT:
            raise ReviewApplicationError("output_alias_unknown")
        # The machine artifact is external to project storage; its original bytes were digest-checked
        # by reconstructing its immutable package rows during validation.
        if output_path.exists():
            try:
                if file_digest(output_path) == (mapped.machine_artifact.size, mapped.machine_artifact.digest):
                    raise ReviewApplicationError("output_is_machine_artifact")
            except ReviewApplicationError:
                raise
            except Exception:
                raise ReviewApplicationError("output_alias_unknown") from None
        if cancellation is not None and cancellation.checkpoint("review_before_reconstruction") is not None:
            raise ReviewApplicationError("cancelled")

        def validate_staged(staged: object) -> None:
            # Staging is the end of reconstruction: the phase cannot be reported after validation starts.
            self._emit(state, JobProgress("reconstructing", "unit", validation.counts.units, validation.counts.units))
            self._emit(state, JobProgress("validating", "step", 0, 1))
            current = self._projects.load_read_only(mapped.project.project_id)
            current_artifact = state.artifacts.get(mapped.machine_artifact.artifact_id)
            if (current.revision != mapped.identity.project_revision
                    or current.source_sha256 != mapped.identity.source_sha256
                    or current.source_size != mapped.identity.source_size
                    or current_artifact.revision != mapped.identity.machine_artifact_revision
                    or current_artifact.digest != mapped.identity.machine_artifact_digest
                    or current_artifact.state is not ArtifactState.READY):
                raise ArtifactIntegrityError("stale_review_identity")
            current_job = state.jobs.get(mapped.machine_artifact.request.job_id).job
            if (current_job.request.command_type != TRANSLATE_COMMAND_TYPE
                    or current_job.state not in (JobState.VALIDATED, JobState.PARTIALLY_COMPLETED)):
                raise ArtifactIntegrityError("stale_review_identity")
            if source.fingerprint() != f"sha256:{mapped.identity.source_sha256}":
                raise ArtifactIntegrityError("source_changed")
            checked = state.format_service.validate("epub", LocalFormatSource(Path(staged)), cancellation=cancellation)
            if checked.failure is not None:
                raise ArtifactIntegrityError(checked.failure.code)
            if not checked.ok:
                raise ArtifactIntegrityError("invalid_epub")
            self._emit(state, JobProgress("validating", "step", 1, 1))
            if cancellation is not None and cancellation.checkpoint("review_before_publication") is not None:
                raise ArtifactCancelledError("cancelled")

        publication = state.publications.publication(state.request, state.owner_token, validate_staged)
        self._emit(state, JobProgress("reconstructing", "unit", 0, validation.counts.units))
        materialized = state.format_service.reconstruct(
            "epub", source,
            ReconstructionRequest(mapped.plan.documents, mapped.plan.translations),
            publication, cancellation=cancellation)
        if materialized.failure is not None or materialized.artifact is None:
            raise ReviewApplicationError(materialized.failure.code if materialized.failure else "reconstruction_failed")
        state.published_artifact = materialized.artifact
        return materialized.artifact

    def complete(self, operation: ReviewOperation, identity: ReviewPackageIdentity, artifact: Artifact,
                 *, reviewed_unit_ids: tuple[str, ...] = (), warnings: tuple[str, ...] = (),
                 counts: ReviewCounts = ReviewCounts()) -> ReviewOperationResult:
        state = self._state(operation)
        if artifact.state is not ArtifactState.READY:
            return self.fail(operation, "artifact_not_ready")
        try:
            self._write_lineage(state, identity, artifact, reviewed_unit_ids)
        except Exception:
            result = self._finish(state, JobState.PARTIALLY_COMPLETED, "provenance_failed",
                                  JobFailure(JobFailureFamily.RESOURCE, "provenance_failed"), counts)
            return result
        try:
            for warning in warnings[:64]:
                self._emit(state, JobWarning(warning, (("count", 1),)))
            state.job = state.jobs.transition(state.job.job_id, JobState.COMPLETED,
                                              expected_revision=state.job.revision)
            state.job = state.jobs.transition(state.job.job_id, JobState.VALIDATING,
                                              expected_revision=state.job.revision)
            return self._finish(state, JobState.VALIDATED,
                                "review_package_exported" if state.command_type == REVIEW_EXPORT_COMMAND_TYPE else "review_applied",
                                None, counts, artifact=artifact)
        except Exception:
            # The final provenance marker and ready artifact are restart-recoverable. Report success
            # here only if the terminal event also reached the ordered stream; otherwise recovery
            # can surface the committed job without inventing a missing event.
            try:
                state.job = state.jobs.get(state.job_id).job
            except Exception:
                pass
            self._close_guard(state)
            expected_code = ("review_package_exported" if state.command_type == REVIEW_EXPORT_COMMAND_TYPE
                             else "review_applied")
            if _has_recorded_terminal_outcome(state, expected_code):
                result = ReviewOperationResult(state.job_id, JobState.VALIDATED,
                                               expected_code,
                                               artifact, True, state.events.replay())
            else:
                result = ReviewOperationResult(state.job_id, state.job.state, "job_commit_pending", None,
                                               False, state.events.replay())
            state.terminal = result if result.committed else None
            return result

    def fail(self, operation: ReviewOperation, code: str, failure: JobFailure | None = None) -> ReviewOperationResult:
        state = self._state(operation)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "review_operation_failed"
        if state.terminal is not None:
            return state.terminal
        if "cancel" in code and state.job.state is JobState.RUNNING and state.published_artifact is None:
            try:
                state.job = state.jobs.transition(state.job.job_id, JobState.CANCELLING,
                                                  expected_revision=state.job.revision)
                # The ordered stream refuses a cancelled outcome that no request and acknowledgement explain.
                self._emit(state, CancellationRequest("cancelled", self._clock()))
                self._emit(state, CancellationAcknowledgement("cancelled", "review_operation"))
                return self._finish(state, JobState.CANCELLED, "cancelled",
                                    JobFailure(JobFailureFamily.CANCELLED, "cancelled"), ReviewCounts())
            except Exception as error:
                raise ReviewApplicationError(_safe_code(error, "job_commit_failed")) from None
        if state.published_artifact is not None:
            return self._finish(state, JobState.PARTIALLY_COMPLETED, "publication_uncommitted",
                                failure or JobFailure(JobFailureFamily.INTEGRITY, code), ReviewCounts())
        return self._finish(state, JobState.FAILED, code,
                            failure or JobFailure(JobFailureFamily.INPUT, code), ReviewCounts())

    @staticmethod
    def _labels(command_type: str) -> tuple[str, str, str]:
        if command_type == REVIEW_EXPORT_COMMAND_TYPE:
            return _REVIEW_EXPORT_ROLE, _REVIEW_EXPORT_STAGE, _CSV_MEDIA_TYPE
        return _REVIEW_APPLY_ROLE, _REVIEW_APPLY_STAGE, _EPUB_MEDIA_TYPE

    def _load_project(self, project_id: str) -> ProjectManifest:
        try:
            return self._projects.load_read_only(project_id)
        except Exception as error:
            raise ReviewApplicationError(_safe_code(error, "project_unavailable")) from None

    def _terminalize(self, jobs: JobRepository, job: object, target: JobState, reason: str) -> object:
        """Settle a job left by a dead review owner through legal lifecycle edges."""
        current = job
        if current.state is JobState.QUEUED:
            current = jobs.transition(current.job_id, JobState.PREFLIGHT, expected_revision=current.revision)
        if current.state is JobState.PREFLIGHT:
            current = jobs.transition(current.job_id, JobState.RUNNING, expected_revision=current.revision)
        if current.state is JobState.COMPLETED:
            current = jobs.transition(current.job_id, JobState.VALIDATING, expected_revision=current.revision)
        if current.state is JobState.CANCELLING:
            summary = JobTerminalSummary("cancelled")
            return jobs.transition(current.job_id, JobState.CANCELLED, expected_revision=current.revision, summary=summary)
        if current.state in (JobState.RUNNING, JobState.VALIDATING):
            summary = JobTerminalSummary(reason)
            legal_target = target
            if current.state is JobState.VALIDATING:
                legal_target = JobState.PARTIALLY_COMPLETED
            return jobs.transition(current.job_id, legal_target, expected_revision=current.revision, summary=summary)
        return current

    @staticmethod
    def _reconcile_success(jobs: JobRepository, job: object) -> object:
        current = job
        if current.lease is not None:
            jobs.release_lease(current.job_id, current.lease.owner_token)
            current = jobs.get(current.job_id).job
        if current.state is JobState.QUEUED:
            current = jobs.transition(current.job_id, JobState.PREFLIGHT, expected_revision=current.revision)
        if current.state is JobState.PREFLIGHT:
            current = jobs.transition(current.job_id, JobState.RUNNING, expected_revision=current.revision)
        if current.state is JobState.RUNNING:
            current = jobs.transition(current.job_id, JobState.COMPLETED, expected_revision=current.revision)
        if current.state is JobState.COMPLETED:
            current = jobs.transition(current.job_id, JobState.VALIDATING, expected_revision=current.revision)
        if current.state is JobState.VALIDATING:
            return jobs.transition(current.job_id, JobState.VALIDATED, expected_revision=current.revision,
                                   summary=JobTerminalSummary("review_applied" if current.request.command_type == REVIEW_APPLY_COMMAND_TYPE
                                                              else "review_package_exported"))
        return current

    def _settle_unstarted_job(self, jobs: JobRepository, job: object) -> None:
        try:
            current = jobs.get(job.job_id).job
            if current.lease is not None:
                jobs.release_lease(current.job_id, current.lease.owner_token)
                current = jobs.get(current.job_id).job
            if not current.terminal:
                self._terminalize(jobs, current, JobState.FAILED, "operation_start_failed")
        except Exception:
            return

    @staticmethod
    def _state(operation: ReviewOperation) -> _OperationState:
        if type(operation) is not ReviewOperation or not isinstance(operation.token, _OperationState):
            raise ReviewApplicationError("invalid_operation")
        return operation.token

    def _write_lineage(self, state: _OperationState, identity: ReviewPackageIdentity, artifact: Artifact,
                       reviewed_unit_ids: tuple[str, ...]) -> None:
        package_value = identity.package_fingerprint
        if state.command_type == REVIEW_EXPORT_COMMAND_TYPE:
            operation, actor, kind = "review_export", ActorKind.SYSTEM, SourceKind.PIPELINE
            package_outcome, unit_outcome, committed_outcome = "review_package_staged", "review_unit_staged", "review_exported"
        else:
            operation, actor, kind = "apply_review", ActorKind.HUMAN, SourceKind.HUMAN
            package_outcome, unit_outcome, committed_outcome = "review_package_validated", "human_edit_validated", "review_applied"
        package_subject = SubjectRef("review_package", 1, package_value)
        package_record = state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, kind, _UNKNOWN_PRODUCER,
            subject=package_subject, outcome_code=package_outcome,
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("package", state.idempotency_key, state.job_id)))
        source_record = state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
            subject=SubjectRef("source_revision", 1, f"sha256:{identity.source_sha256}"), outcome_code="source_verified",
            parents=(package_record.record_id,),
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("source", state.idempotency_key, state.job_id)))
        for unit_id in reviewed_unit_ids:
            parents: tuple[str, ...] = ()
            if state.command_type == REVIEW_APPLY_COMMAND_TYPE:
                try:
                    machine = state.provenance.list_by_subject(SubjectRef("translation_unit", 1, unit_id), limit=64)
                    if machine.truncated:
                        raise ReviewApplicationError("unit_provenance_history_incomplete")
                    match = next((node.record for node in machine.nodes
                                  if node.record.request.operation == "translate_unit"
                                  and node.record.request.source_kind in (SourceKind.PROVIDER, SourceKind.CACHE,
                                                                        SourceKind.TRANSLATION_MEMORY, SourceKind.PIPELINE)
                                  and identity.machine_artifact_id in node.record.request.artifacts), None)
                    if match is None:
                        raise ReviewApplicationError("unit_provenance_unavailable")
                    parents = (match.record_id,)
                except ProvenanceError as error:
                    raise ReviewApplicationError(_safe_code(error, "unit_provenance_unavailable")) from None
            state.provenance.append(ProvenanceRequest(
                state.project.project_id, state.job_id, "review_unit", actor, kind, _UNKNOWN_PRODUCER,
                subject=SubjectRef("translation_unit", 1, unit_id), outcome_code=unit_outcome,
                parents=tuple(sorted({*parents, package_record.record_id})),
                artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
                operation_id=state.operation_id,
                idempotency_key=_lineage_key("unit", state.idempotency_key, state.job_id, unit_id)))
        state.provenance.append(ProvenanceRequest(
            state.project.project_id, state.job_id, operation, actor, SourceKind.VALIDATION, _UNKNOWN_PRODUCER,
            subject=_operation_subject(state.idempotency_key), outcome_code=committed_outcome,
            parents=(package_record.record_id, source_record.record_id),
            artifacts=tuple(sorted({artifact.artifact_id, identity.machine_artifact_id or ""} - {""})),
            operation_id=state.operation_id, idempotency_key=_lineage_key("complete", state.idempotency_key, state.job_id)))

    def _finish(self, state: _OperationState, target: JobState, code: str, failure: JobFailure | None,
                counts: ReviewCounts, *, artifact: Artifact | None = None) -> ReviewOperationResult:
        if state.terminal is not None:
            return state.terminal
        job = state.job
        summary = JobTerminalSummary(code, (("documents", counts.documents), ("units", counts.units),
                                             ("reviewed", counts.reviewed), ("pending", counts.pending)))
        # Only a FAILED outcome carries a failure; the other terminal states state their reason in the summary.
        outcome = JobOutcome(target, summary, failure if target is JobState.FAILED else None)
        try:
            job = state.jobs.transition(job.job_id, target, expected_revision=job.revision, summary=summary)
        except Exception as error:
            self._close_guard(state)
            raise ReviewApplicationError(_safe_code(error, "job_commit_failed")) from None
        state.job = job
        self._emit(state, outcome)
        result = ReviewOperationResult(state.job_id, target, code,
                                       artifact if target is JobState.VALIDATED else None,
                                       target is JobState.VALIDATED and artifact is not None,
                                       state.events.replay(), failure)
        state.terminal = result
        self._close_guard(state)
        return result

    def _emit(self, state: _OperationState, payload: object) -> None:
        event = JobEvent(self._new_id(), state.job_id, state.event_sequence + 1, self._clock(),
                         JobCorrelation(state.project.project_id, state.operation_id), payload)
        state.events.emit(event)
        state.event_sequence += 1

    @staticmethod
    def _close_guard(state: _OperationState) -> None:
        if state.guard_open:
            state.guard_open = False
            state.guard.__exit__(None, None, None)


@dataclass(frozen=True, repr=False)
class ReviewUseCases:
    export: ExportReviewPackage
    validate: ValidateReviewPackage
    apply: ApplyReviewPackage
    operations: LocalReviewOperations

    def __repr__(self) -> str:
        return "ReviewUseCases()"

    def cancellation(self) -> CancellationSource:
        return self.operations.cancellation()


def epub_review_formats() -> FormatService:
    registry = FormatRegistry()
    from .epub_format import EPUB_REGISTRATION
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def open_review_use_cases(processing_dir: Path, *, source: FormatSource | None = None,
                          source_path: Path | str | None = None,
                          formats: FormatService | None = None,
                          clock: Callable[[], datetime] = _utc_now,
                          new_id: Callable[[], str] = lambda: str(uuid4())) -> ReviewUseCases:
    """Open framework-free use cases; repositories/adapters stay in this composition module."""
    format_service = formats or epub_review_formats()
    if source is None and source_path is not None:
        source = LocalFormatSource(source_path)
    packages = LocalReviewPackagePort(processing_dir, format_service, source=source)
    operations = LocalReviewOperations(processing_dir, format_service, clock=clock, new_id=new_id)
    ports = ReviewUseCasePorts(packages, operations)
    return ReviewUseCases(ExportReviewPackage(ports), ValidateReviewPackage(ports),
                         ApplyReviewPackage(ports), operations)
