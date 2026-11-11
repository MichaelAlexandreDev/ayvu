"""Bounded, identity-bound review packages.

The CSV is a local interchange file, not an instruction to parse markup. Every
reviewed cell stays plain text; package-issued placeholders can only select
protected parts already present in the matching ``ContentDocument``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from ..artifacts import Artifact, ArtifactState
from ..content import ContentDocument, TranslationUnit
from ..formats import FormatDescriptor
from ..projects import ProjectManifest
from .exchange_model import (
    MAX_REVIEW_CELL_CHARS,
    MAX_REVIEW_DOCUMENTS,
    MAX_REVIEW_PARTS_PER_UNIT,
    MAX_REVIEW_ROWS,
    REVIEW_CODEC_VERSION,
    REVIEW_PACKAGE_SCHEMA_VERSION,
    ReviewExchangeError,
    ReviewPackage,
    ReviewPackageHeader,
    ReviewPackageRow,
    _FORMAT_VERSION,
    _SLUG,
    _hash_text,
    _header_fields,
    _locator_fingerprint,
    _validate_package_shape,
    canonical_json,
)
from .exchange_parts import (
    _machine_review_text,
    _placeholder,
    _plain_local_tokens,
    _source_review_text,
    _unit_owner,
)


def _project_header(project: ProjectManifest, machine_artifact: Artifact, descriptor: FormatDescriptor,
                    format_id: str, format_version: str) -> ReviewPackageHeader:
    if type(project) is not ProjectManifest or type(machine_artifact) is not Artifact or type(descriptor) is not FormatDescriptor:
        raise ReviewExchangeError("invalid_export_context")
    if machine_artifact.state is not ArtifactState.READY or machine_artifact.size is None or machine_artifact.digest is None:
        raise ReviewExchangeError("machine_artifact_not_ready")
    if machine_artifact.request.project_id != project.project_id:
        raise ReviewExchangeError("ownership_mismatch")
    if not isinstance(format_id, str) or not _SLUG.fullmatch(format_id):
        raise ReviewExchangeError("invalid_format_identity")
    if not isinstance(format_version, str) or not _FORMAT_VERSION.fullmatch(format_version):
        raise ReviewExchangeError("invalid_format_identity")
    if descriptor.adapter.namespace != format_id:
        raise ReviewExchangeError("incompatible_format")
    if format_version not in descriptor.format_versions:
        raise ReviewExchangeError("incompatible_format")
    return ReviewPackageHeader(
        schema_version=REVIEW_PACKAGE_SCHEMA_VERSION,
        codec_version=REVIEW_CODEC_VERSION,
        package_id="",
        project_id=project.project_id,
        project_revision=project.revision,
        source_sha256=project.source_sha256,
        source_size=project.source_size,
        machine_artifact_id=machine_artifact.artifact_id,
        machine_artifact_schema_version=machine_artifact.schema_version,
        machine_artifact_revision=machine_artifact.revision,
        machine_artifact_digest=machine_artifact.digest,
        machine_artifact_size=machine_artifact.size,
        machine_role=machine_artifact.request.role,
        machine_stage=machine_artifact.request.stage,
        machine_stage_version=machine_artifact.request.stage_version,
        machine_input_fingerprint=machine_artifact.request.input_fingerprint,
        machine_media_type=machine_artifact.request.media_type,
        format_id=format_id,
        format_version=format_version,
        format_contract_version=descriptor.contract_version,
        adapter_namespace=descriptor.adapter.namespace,
        adapter_version=descriptor.adapter.adapter_version,
        segmenter_version=descriptor.adapter.segmenter_version,
    )


def _base_rows(documents: Sequence[ContentDocument], machine_translations: Mapping[str, str],
               descriptor: FormatDescriptor, source_fingerprint: str, project_id: str
               ) -> list[tuple[ContentDocument, TranslationUnit, str, str, str]]:
    if type(documents) not in (tuple, list) or len(documents) > MAX_REVIEW_DOCUMENTS:
        raise ReviewExchangeError("too_many_documents")
    if not isinstance(machine_translations, Mapping):
        raise ReviewExchangeError("invalid_machine_translations")
    by_key: dict[str, tuple[ContentDocument, TranslationUnit, str, str, str]] = {}
    doc_ids: set[str] = set()
    for document in documents:
        if type(document) is not ContentDocument:
            raise ReviewExchangeError("invalid_document")
        if document.project_id != project_id:
            raise ReviewExchangeError("ownership_mismatch")
        if document.adapter != descriptor.adapter:
            raise ReviewExchangeError("incompatible_format")
        if document.source_fingerprint != source_fingerprint:
            raise ReviewExchangeError("source_mismatch")
        if document.document_id in doc_ids:
            raise ReviewExchangeError("duplicate_document")
        doc_ids.add(document.document_id)
        if len(document.units) > MAX_REVIEW_ROWS:
            raise ReviewExchangeError("too_many_rows")
        for unit in document.units:
            if (unit.content.locator.schema, unit.content.locator.schema_version) not in descriptor.locators:
                raise ReviewExchangeError("incompatible_locator")
            if len(unit.content.protected_parts) > MAX_REVIEW_PARTS_PER_UNIT:
                raise ReviewExchangeError("too_many_protected_parts")
            if unit.unit_id in by_key:
                raise ReviewExchangeError("duplicate_unit")
            raw_machine = machine_translations.get(unit.unit_id)
            if not isinstance(raw_machine, str) or not raw_machine:
                raise ReviewExchangeError("missing_machine_translation")
            if len(raw_machine) > MAX_REVIEW_CELL_CHARS or "\x00" in raw_machine:
                raise ReviewExchangeError("machine_translation_too_large")
            _plain_local_tokens(raw_machine, len(unit.content.protected_parts))
            machine_fp = _hash_text(raw_machine)
            by_key[unit.unit_id] = (document, unit, raw_machine, machine_fp, _hash_text(unit.source_text))
            if len(by_key) > MAX_REVIEW_ROWS:
                raise ReviewExchangeError("too_many_rows")
    if set(machine_translations) != set(by_key):
        raise ReviewExchangeError("machine_translation_set_mismatch")
    return [by_key[key] for key in sorted(by_key)]


def _package_id(header: ReviewPackageHeader, immutable_rows: Sequence[tuple[str, ...]]) -> str:
    payload = canonical_json({"header": _header_fields(header, include_package_id=False),
                              "units": [list(item) for item in immutable_rows]})
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return f"rpk:v1:{digest}"


def export_review_package(*, project: ProjectManifest, machine_artifact: Artifact, descriptor: FormatDescriptor,
                          format_version: str, documents: Sequence[ContentDocument],
                          machine_translations: Mapping[str, str], format_id: str | None = None) -> ReviewPackage:
    """Build a deterministic package from one durable project, source, artifact and extraction."""
    if type(descriptor) is not FormatDescriptor:
        raise ReviewExchangeError("invalid_export_context")
    selected_format_id = descriptor.adapter.namespace if format_id is None else format_id
    header = _project_header(project, machine_artifact, descriptor, selected_format_id, format_version)
    source_fingerprint = f"sha256:{project.source_sha256}"
    base = _base_rows(documents, machine_translations, descriptor, source_fingerprint, project.project_id)
    immutable: list[tuple[str, ...]] = []
    for document, unit, machine_text, machine_fp, source_fp in base:
        immutable.append((document.document_id, document.revision_id, unit.unit_id, unit.source_digest,
                          _locator_fingerprint(unit), source_fp, machine_fp))
    package_id = _package_id(header, immutable)
    header = ReviewPackageHeader(**{**header.__dict__, "package_id": package_id})

    rows: list[ReviewPackageRow] = []
    for document, unit, _machine_text, _machine_fp, _source_fp in base:
        machine_text = _machine_review_text(machine_translations[unit.unit_id], package_id,
                                            unit.content.protected_parts, unit_id=unit.unit_id)
        source_text = _source_review_text(unit, package_id)
        protected = tuple((_placeholder(_unit_owner(package_id, unit.unit_id), index), part.kind)
                          for index, part in enumerate(unit.content.protected_parts))
        rows.append(ReviewPackageRow(
            document_ref=document.document_id,
            document_revision_id=document.revision_id,
            locator_schema=unit.content.locator.schema,
            locator_schema_version=unit.content.locator.schema_version,
            locator_fingerprint=_locator_fingerprint(unit),
            unit_id=unit.unit_id,
            source_digest=unit.source_digest,
            source_text_fingerprint=_hash_text(source_text),
            source_text=source_text,
            machine_translation=machine_text,
            machine_fingerprint=_hash_text(machine_text),
            reviewed_text=machine_text,
            protected_parts=protected,
            review_status="pending",
            reviewer_note=None,
        ))
    package = ReviewPackage(header, tuple(sorted(rows, key=lambda row: (row.document_ref, row.unit_id))))
    _validate_package_shape(package, check_duplicates=True)
    return package
