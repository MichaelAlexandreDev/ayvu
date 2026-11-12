"""Read-only package export, inspection and validation against local project stores."""
from __future__ import annotations

import re
from pathlib import Path

from ..artifacts import Artifact, ArtifactState
from ..artifacts import ArtifactService
from ..artifacts.store import LocalArtifactPayloads, SqliteArtifactRepository
from ..formats import AliasVerdict
from ..formats.local import LocalFormatSource, LocalOutputGuard
from ..formats import FormatService, FormatSource
from ..jobs import JobState
from ..jobs import JobRepository
from ..jobs.store import SqliteJobRepository
from ..projects import ProjectManifest
from ..projects.schema import project_migration_runner
from ..projects.store import ProjectJsonRepository
from ..provenance import ProvenanceError, SourceKind, SubjectRef
from ..provenance.store import SqliteProvenanceRepository
from . import ReviewApplicationError, ReviewCounts, ReviewPackageExport, ReviewPackageIdentity, ReviewValidationMaterial
from . import REVIEW_PACKAGE_SCHEMA_VERSION, ReviewExchangeError, ReviewPackage, decode_review_package, encode_review_package, export_review_package
from . import ReviewImportError, read_review_csv_bytes
from ..translation import TranslateProjectResult
from ..translation import OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, TRANSLATE_COMMAND_TYPE
from ..translation import STATUS_CACHE, STATUS_ERROR, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED
from ..kernel import CancellationToken

from .local_files import _MAX_PACKAGE_BYTES, _fingerprint, _safe_code, destination_fingerprint
from .local_context import _EPUB_MEDIA_TYPE, _MappedReview, _map_review_context


_MACHINE_LINEAGE_KINDS = frozenset((SourceKind.PROVIDER, SourceKind.CACHE, SourceKind.TRANSLATION_MEMORY, SourceKind.PIPELINE))


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
            descriptor=self._formats.descriptor("epub"),
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
        return _map_review_context(self, package, identity, source, cancellation)

    @staticmethod
    def _machine_translations(package: ReviewPackage) -> dict[str, str]:
        translations: dict[str, str] = {}
        for row in package.rows:
            text = row.machine_translation
            for index, (placeholder, _kind) in enumerate(row.protected_parts):
                text = text.replace(placeholder, f"__AYVU_TAG_{index}__")
            translations[row.unit_id] = text
        return translations
