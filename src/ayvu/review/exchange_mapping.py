from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..artifacts import Artifact
from ..content import ContentDocument, TranslationUnit
from ..formats import FormatDescriptor, UnitTranslation
from ..projects import ProjectManifest
from .exchange import _base_rows, _package_id, _project_header
from .exchange_model import (
    ReviewExchangeError,
    ReviewPackage,
    ReviewPackageHeader,
    ReviewPackageRow,
    ReviewReconstructionPlan,
    _hash_text,
    _header_fields,
    _locator_fingerprint,
    _validate_package_shape,
)
from .exchange_parts import (
    _machine_review_text,
    _placeholder,
    _source_review_text,
    _to_local_tokens,
    _unit_owner,
    _validate_local_tokens_for_unit,
    _validate_review_placeholders,
)


def _expected_context(project: ProjectManifest, machine_artifact: Artifact, descriptor: FormatDescriptor,
                      format_version: str, documents: Sequence[ContentDocument], machine_translations: Mapping[str, str],
                      header: ReviewPackageHeader) -> tuple[list[tuple[ContentDocument, TranslationUnit, str, str, str]], str]:
    expected_header = _project_header(project, machine_artifact, descriptor, header.format_id, format_version)
    expected_values = _header_fields(expected_header, include_package_id=False)
    actual_values = _header_fields(header, include_package_id=False)
    if actual_values != expected_values:
        if header.project_id != project.project_id:
            raise ReviewExchangeError("ownership_mismatch")
        if header.project_revision != project.revision:
            raise ReviewExchangeError("stale_project_revision")
        if (header.source_sha256, header.source_size) != (project.source_sha256, project.source_size):
            raise ReviewExchangeError("source_mismatch")
        if header.machine_artifact_id != machine_artifact.artifact_id:
            raise ReviewExchangeError("machine_artifact_mismatch")
        if header.machine_artifact_revision != machine_artifact.revision:
            raise ReviewExchangeError("stale_machine_artifact")
        if (header.machine_artifact_digest, header.machine_artifact_size) != (machine_artifact.digest, machine_artifact.size):
            raise ReviewExchangeError("machine_artifact_mismatch")
        if (header.format_contract_version != descriptor.contract_version
                or header.format_id != descriptor.adapter.namespace or header.format_version != format_version
                or header.adapter_namespace != descriptor.adapter.namespace
                or header.adapter_version != descriptor.adapter.adapter_version
                or header.segmenter_version != descriptor.adapter.segmenter_version):
            raise ReviewExchangeError("incompatible_format")
        raise ReviewExchangeError("stale_or_incompatible_package")
    if format_version not in descriptor.format_versions:
        raise ReviewExchangeError("incompatible_format")
    base = _base_rows(documents, machine_translations, descriptor, f"sha256:{project.source_sha256}", project.project_id)
    immutable = [(doc.document_id, doc.revision_id, unit.unit_id, unit.source_digest, _locator_fingerprint(unit), source_fp, machine_fp)
                 for doc, unit, _machine, machine_fp, source_fp in base]
    expected_package_id = _package_id(expected_header, immutable)
    if header.package_id != expected_package_id:
        raise ReviewExchangeError("stale_or_incompatible_package")
    return base, expected_package_id


def map_review_package(package: ReviewPackage, *, project: ProjectManifest, machine_artifact: Artifact,
                       descriptor: FormatDescriptor, format_version: str, documents: Sequence[ContentDocument],
                       machine_translations: Mapping[str, str]) -> ReviewReconstructionPlan:
    """Validate the complete exchange and return stable-unit structural inputs, all or nothing."""
    _validate_package_shape(package, check_duplicates=True)
    header = package.header
    base, package_id = _expected_context(project, machine_artifact, descriptor, format_version, documents,
                                         machine_translations, header)
    expected_by_key: dict[tuple[str, str], tuple[ContentDocument, TranslationUnit, str, str, str]] = {
        (item[0].document_id, item[1].unit_id): item for item in base
    }
    row_by_key: dict[tuple[str, str], ReviewPackageRow] = {}
    expected_document_ids = {doc.document_id for doc in documents}
    for index, row in enumerate(package.rows):
        if row.document_ref not in expected_document_ids:
            raise ReviewExchangeError("unknown_document", row_index=index)
        key = (row.document_ref, row.unit_id)
        if key not in expected_by_key:
            raise ReviewExchangeError("unknown_unit", row_index=index)
        row_by_key[key] = row
    missing = set(expected_by_key) - set(row_by_key)
    if missing:
        raise ReviewExchangeError("missing_unit")
    extra = set(row_by_key) - set(expected_by_key)
    if extra:
        raise ReviewExchangeError("unknown_unit")
    if set(row_by_key) != set(expected_by_key):
        raise ReviewExchangeError("incomplete_package")

    staged: list[UnitTranslation] = []
    for key in sorted(expected_by_key):
        document, unit, machine_text, machine_fp, source_fp = expected_by_key[key]
        row = row_by_key[key]
        if row.document_revision_id != document.revision_id:
            raise ReviewExchangeError("stale_document_revision")
        locator = unit.content.locator
        if (row.locator_schema, row.locator_schema_version, row.locator_fingerprint) != (
                locator.schema, locator.schema_version, _locator_fingerprint(unit)):
            raise ReviewExchangeError("incompatible_locator")
        if row.source_digest != unit.source_digest or row.source_text_fingerprint != _hash_text(_source_review_text(unit, package_id)):
            raise ReviewExchangeError("source_mismatch")
        if row.source_text != _source_review_text(unit, package_id):
            raise ReviewExchangeError("source_mismatch")
        expected_machine = _machine_review_text(machine_translations[unit.unit_id], package_id,
                                                unit.content.protected_parts, unit_id=unit.unit_id)
        if row.machine_fingerprint != _hash_text(expected_machine) or row.machine_translation != expected_machine:
            raise ReviewExchangeError("machine_mismatch")
        if row.machine_fingerprint != _hash_text(row.machine_translation):
            raise ReviewExchangeError("machine_mismatch")
        expected_parts = tuple((_placeholder(_unit_owner(package_id, unit.unit_id), index), part.kind)
                               for index, part in enumerate(unit.content.protected_parts))
        if row.protected_parts != expected_parts:
            raise ReviewExchangeError("protected_contract_mismatch")
        if row.review_status == "rejected":
            raise ReviewExchangeError("review_rejected")
        # A pending row keeps the machine translation and is never recorded as human work.
        # An edit left unmarked stops the package, so a correction is never silently dropped.
        if row.review_status == "pending" and row.reviewed_text != row.machine_translation:
            raise ReviewExchangeError("pending_row_edited")
        if not row.reviewed_text:
            raise ReviewExchangeError("empty_review_text")
        markers = [marker for marker, _kind in expected_parts]
        _validate_review_placeholders(row.reviewed_text, markers)
        mapped = _to_local_tokens(row.reviewed_text, markers)
        _validate_local_tokens_for_unit(mapped, unit)
        staged.append(UnitTranslation(unit.unit_id, mapped))

    if len(staged) != len(expected_by_key):
        raise ReviewExchangeError("incomplete_package")
    if not any(row.review_status == "reviewed" for row in row_by_key.values()):
        raise ReviewExchangeError("review_not_approved")
    doc_by_id = {document.document_id: document for document in documents}
    ordered_documents = tuple(doc_by_id[key] for key in sorted(doc_by_id))
    return ReviewReconstructionPlan(ordered_documents, tuple(staged))
