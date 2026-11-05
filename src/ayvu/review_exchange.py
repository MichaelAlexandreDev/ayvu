"""Bounded, identity-bound review packages.

The CSV is a local interchange file, not an instruction to parse markup. Every
reviewed cell stays plain text; package-issued placeholders can only select
protected parts already present in the matching ``ContentDocument``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact import Artifact, ArtifactState
from .content import ContentDocument, TranslationUnit
from .format_adapter import FormatDescriptor, UnitTranslation
from .project import ProjectManifest
from .translation_protection import INTERNAL_MARKER_PATTERN, TAG_TOKEN_PATTERN


REVIEW_PACKAGE_SCHEMA_VERSION = 2
REVIEW_CODEC_VERSION = 1
REVIEW_CSV_PREAMBLE = "AYVU-REVIEW-PACKAGE"
REVIEW_CSV_COLUMNS = (
    "document_ref",
    "document_revision_id",
    "locator_schema",
    "locator_schema_version",
    "locator_fingerprint",
    "unit_id",
    "source_digest",
    "source_text_fingerprint",
    "source_text",
    "machine_translation",
    "machine_fingerprint",
    "reviewed_text",
    "protected_parts",
    "review_status",
    "reviewer_note",
)

# These limits apply before a path-backed input is decoded or parsed.
MAX_REVIEW_RAW_BYTES = 64 * 1024 * 1024
MAX_REVIEW_ROWS = 100_000
MAX_REVIEW_COLUMNS = len(REVIEW_CSV_COLUMNS)
MAX_REVIEW_CELL_CHARS = 64 * 1024
MAX_REVIEW_TOTAL_CHARS = 32 * 1024 * 1024
MAX_REVIEW_METADATA_CHARS = 16 * 1024
MAX_REVIEW_METADATA_ITEMS = 32
MAX_REVIEW_METADATA_DEPTH = 3
MAX_REVIEW_DOCUMENTS = 32_768
MAX_REVIEW_PARTS_PER_UNIT = 128
MAX_REVIEW_NOTE_CHARS = 8192
MAX_LEGACY_REVIEW_RAW_BYTES = 32 * 1024 * 1024
MAX_LEGACY_REVIEW_ROWS = 100_000
MAX_LEGACY_REVIEW_COLUMNS = 64
MAX_LEGACY_REVIEW_CELL_CHARS = 64 * 1024
MAX_LEGACY_REVIEW_TOTAL_CHARS = 16 * 1024 * 1024

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FORMAT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_PACKAGE_ID = re.compile(r"rpk:v1:[0-9a-f]{64}\Z")
_PLACEHOLDER = re.compile(r"__AYVU_REVIEW_PART_([0-9a-f]{32})_([0-9]{4})__")
_PLACEHOLDER_LIKE = "__AYVU_REVIEW_PART_"
_FORMULA_ESCAPE_START = "'⟪AYVU-FORMULA-ESCAPE-v1⟫"
_LOCAL_TOKEN = re.compile(r"__AYVU_TAG_(\d+)__")
_SEMICOLON_PREAMBLE = re.compile(r'"?' + REVIEW_CSV_PREAMBLE + r'"?;')


class ReviewExchangeError(ValueError):
    """A bounded failure with no imported text, paths, or raw parser details."""

    def __init__(self, code: str, *, row_index: int | None = None) -> None:
        safe_code = code if isinstance(code, str) and _SLUG.fullmatch(code) else "invalid_review_exchange"
        safe_index = row_index if type(row_index) is int and 0 <= row_index < MAX_REVIEW_ROWS else None
        self.code = safe_code
        self.row_index = safe_index
        self.failure = ReviewExchangeFailure(safe_code, safe_index)
        super().__init__(safe_code)


@dataclass(frozen=True, repr=False)
class ReviewExchangeFailure:
    code: str
    row_index: int | None = None

    def __repr__(self) -> str:
        return f"ReviewExchangeFailure(code={self.code!r}, row_index={self.row_index!r})"


@dataclass(frozen=True, repr=False)
class ReviewPackageHeader:
    schema_version: int
    codec_version: int
    package_id: str
    project_id: str
    project_revision: int
    source_sha256: str
    source_size: int
    machine_artifact_id: str
    machine_artifact_schema_version: int
    machine_artifact_revision: int
    machine_artifact_digest: str
    machine_artifact_size: int
    machine_role: str
    machine_stage: str
    machine_stage_version: int
    machine_input_fingerprint: str
    machine_media_type: str
    format_id: str
    format_version: str
    format_contract_version: int
    adapter_namespace: str
    adapter_version: int
    segmenter_version: int

    def __repr__(self) -> str:
        return "ReviewPackageHeader(schema_version=2, codec_version=1)"


@dataclass(frozen=True, repr=False)
class ReviewPackageRow:
    document_ref: str
    document_revision_id: str
    locator_schema: str
    locator_schema_version: int
    locator_fingerprint: str
    unit_id: str
    source_digest: str
    source_text_fingerprint: str
    source_text: str
    machine_translation: str
    machine_fingerprint: str
    reviewed_text: str
    protected_parts: tuple[tuple[str, str], ...]
    review_status: str = "pending"
    reviewer_note: str | None = None

    def __repr__(self) -> str:
        return "ReviewPackageRow(<sensitive fields redacted>)"


@dataclass(frozen=True, repr=False)
class ReviewPackage:
    header: ReviewPackageHeader
    rows: tuple[ReviewPackageRow, ...]

    def __repr__(self) -> str:
        return f"ReviewPackage(schema_version={self.header.schema_version}, rows={len(self.rows)})"


@dataclass(frozen=True, repr=False)
class ReviewReconstructionPlan:
    """Validated structural inputs for the existing format reconstructor."""

    documents: tuple[ContentDocument, ...]
    translations: tuple[UnitTranslation, ...]

    def __repr__(self) -> str:
        return f"ReviewReconstructionPlan(documents={len(self.documents)}, translations={len(self.translations)})"


def canonical_json(value: object) -> str:
    """The v1 codec's canonical JSON spelling used in package metadata and part contracts."""
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ReviewExchangeError("invalid_package_value") from None


def _hash_text(value: str) -> str:
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        raise ReviewExchangeError("invalid_text") from None
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _locator_fingerprint(unit: TranslationUnit) -> str:
    locator = unit.content.locator
    payload = canonical_json([locator.schema, locator.schema_version, locator.value]).encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _plain_local_tokens(text: str, part_count: int) -> str:
    """Reject damaged, missing, repeated, invented, or reordered local reconstruction tokens."""
    found = list(_LOCAL_TOKEN.finditer(text))
    tokens = [match.group(0) for match in found]
    expected = [f"__AYVU_TAG_{index}__" for index in range(part_count)]
    if tokens != expected:
        if len(set(tokens)) != len(tokens):
            raise ReviewExchangeError("duplicate_placeholder")
        if any(token not in expected for token in tokens):
            raise ReviewExchangeError("unknown_placeholder")
        if len(tokens) != len(expected):
            raise ReviewExchangeError("missing_placeholder")
        raise ReviewExchangeError("invalid_placeholder_order")
    stripped = _LOCAL_TOKEN.sub("", text)
    if INTERNAL_MARKER_PATTERN.search(stripped):
        raise ReviewExchangeError("malformed_placeholder")
    return text


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


def _header_fields(header: ReviewPackageHeader, *, include_package_id: bool = True) -> dict[str, Any]:
    result = {
        "adapter": {
            "namespace": header.adapter_namespace,
            "adapter_version": header.adapter_version,
            "segmenter_version": header.segmenter_version,
        },
        "codec_version": header.codec_version,
        "format": {
            "contract_version": header.format_contract_version,
            "id": header.format_id,
            "version": header.format_version,
        },
        "machine_artifact": {
            "artifact_id": header.machine_artifact_id,
            "digest": header.machine_artifact_digest,
            "input_fingerprint": header.machine_input_fingerprint,
            "media_type": header.machine_media_type,
            "revision": header.machine_artifact_revision,
            "role": header.machine_role,
            "schema_version": header.machine_artifact_schema_version,
            "size": header.machine_artifact_size,
            "stage": header.machine_stage,
            "stage_version": header.machine_stage_version,
        },
        "project": {"id": header.project_id, "revision": header.project_revision},
        "schema_version": header.schema_version,
        "source": {"sha256": header.source_sha256, "size": header.source_size},
    }
    if include_package_id:
        result["package_id"] = header.package_id
    return result


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


def _source_review_text(unit: TranslationUnit, package_id: str) -> str:
    text = unit.source_text
    result: list[str] = []
    cursor = 0
    owner = _unit_owner(package_id, unit.unit_id)
    for index, part in enumerate(unit.content.protected_parts):
        plain = text[cursor:part.start]
        if TAG_TOKEN_PATTERN.search(plain) or _PLACEHOLDER_LIKE in plain:
            raise ReviewExchangeError("reserved_placeholder_in_source")
        result.extend((plain, _placeholder(owner, index)))
        cursor = part.end
    plain = text[cursor:]
    if TAG_TOKEN_PATTERN.search(plain) or _PLACEHOLDER_LIKE in plain:
        raise ReviewExchangeError("reserved_placeholder_in_source")
    result.append(plain)
    return "".join(result)


def _machine_review_text(text: str, package_id: str, parts: Sequence[object], *, unit_id: str = "") -> str:
    owner = _unit_owner(package_id, unit_id)
    expected = {f"__AYVU_TAG_{index}__": _placeholder(owner, index) for index in range(len(parts))}
    return _LOCAL_TOKEN.sub(lambda match: expected.get(match.group(0), match.group(0)), text)


def _unit_owner(package_id: str, unit_id: str) -> str:
    return hashlib.sha256(f"{package_id}\0{unit_id}".encode("utf-8")).hexdigest()[:32]


def _placeholder(owner: str, index: int) -> str:
    return f"__AYVU_REVIEW_PART_{owner}_{index:04d}__"


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


def neutralize_formula_cell(value: str) -> str:
    """Prefix formula triggers; double the reserved prefix when it is user text."""
    if not isinstance(value, str):
        raise ReviewExchangeError("invalid_cell")
    if value.startswith(_FORMULA_ESCAPE_START):
        return _FORMULA_ESCAPE_START + value
    dangerous = (value.startswith(("\t", "\r", "\n"))
                 or re.match(r"\A[ \t\r\n]*[=+\-@]", value) is not None)
    return _FORMULA_ESCAPE_START + value if dangerous else value


def restore_formula_cell(value: str) -> str:
    """Remove one exact prefix, so ordinary apostrophes and similar text stay intact."""
    return value[len(_FORMULA_ESCAPE_START):] if value.startswith(_FORMULA_ESCAPE_START) else value


def _nullable_note(value: str | None) -> str:
    if value is None:
        return r"\N"
    if value.startswith("\\"):
        return "\\" + value
    return value


def _decode_nullable_note(value: str) -> str | None:
    if value == r"\N":
        return None
    if value.startswith("\\\\"):
        return value[1:]
    return value


def _protected_json(parts: tuple[tuple[str, str], ...]) -> str:
    return canonical_json([{"kind": kind, "placeholder": marker} for marker, kind in parts])


def _row_cells(row: ReviewPackageRow) -> list[str]:
    return [
        row.document_ref,
        row.document_revision_id,
        row.locator_schema,
        str(row.locator_schema_version),
        row.locator_fingerprint,
        row.unit_id,
        row.source_digest,
        row.source_text_fingerprint,
        row.source_text,
        row.machine_translation,
        row.machine_fingerprint,
        row.reviewed_text,
        _protected_json(row.protected_parts),
        row.review_status,
        _nullable_note(row.reviewer_note),
    ]


def _validate_package_shape(package: ReviewPackage, *, check_duplicates: bool) -> None:
    if type(package) is not ReviewPackage or type(package.header) is not ReviewPackageHeader or type(package.rows) is not tuple:
        raise ReviewExchangeError("invalid_package")
    _validate_header(package.header)
    if len(package.rows) > MAX_REVIEW_ROWS:
        raise ReviewExchangeError("too_many_rows")
    seen: dict[tuple[str, str], ReviewPackageRow] = {}
    documents: set[str] = set()
    for index, row in enumerate(package.rows):
        if type(row) is not ReviewPackageRow:
            raise ReviewExchangeError("invalid_row", row_index=index)
        _validate_row(row, index)
        documents.add(row.document_ref)
        key = (row.document_ref, row.unit_id)
        if key in seen and check_duplicates:
            old = seen[key]
            code = "duplicate_unit" if old == row else "conflicting_duplicate"
            raise ReviewExchangeError(code, row_index=index)
        seen[key] = row
        if len(documents) > MAX_REVIEW_DOCUMENTS:
            raise ReviewExchangeError("too_many_documents", row_index=index)


def _validate_header(header: ReviewPackageHeader) -> None:
    if type(header.schema_version) is not int or header.schema_version != REVIEW_PACKAGE_SCHEMA_VERSION:
        raise ReviewExchangeError("unsupported_schema_version")
    if type(header.codec_version) is not int or header.codec_version != REVIEW_CODEC_VERSION:
        raise ReviewExchangeError("unsupported_codec_version")
    if not isinstance(header.package_id, str) or not _PACKAGE_ID.fullmatch(header.package_id):
        raise ReviewExchangeError("invalid_package_id")
    if (not isinstance(header.project_id, str) or not _UUID.fullmatch(header.project_id)
            or not isinstance(header.machine_artifact_id, str) or not _UUID.fullmatch(header.machine_artifact_id)):
        raise ReviewExchangeError("invalid_identity")
    if type(header.project_revision) is not int or header.project_revision < 1:
        raise ReviewExchangeError("invalid_revision")
    if (not isinstance(header.source_sha256, str) or not _HEX.fullmatch(header.source_sha256)
            or type(header.source_size) is not int or header.source_size < 0):
        raise ReviewExchangeError("invalid_source_identity")
    if (not isinstance(header.machine_artifact_digest, str) or not _DIGEST.fullmatch(header.machine_artifact_digest)
            or not isinstance(header.machine_input_fingerprint, str) or not _DIGEST.fullmatch(header.machine_input_fingerprint)):
        raise ReviewExchangeError("invalid_machine_identity")
    if (type(header.machine_artifact_size) is not int or header.machine_artifact_size < 0
            or type(header.machine_artifact_revision) is not int or header.machine_artifact_revision < 1
            or type(header.machine_artifact_schema_version) is not int or header.machine_artifact_schema_version < 1
            or type(header.machine_stage_version) is not int or header.machine_stage_version < 1):
        raise ReviewExchangeError("invalid_machine_identity")
    if (not isinstance(header.machine_role, str) or not _SLUG.fullmatch(header.machine_role)
            or not isinstance(header.machine_stage, str) or not _SLUG.fullmatch(header.machine_stage)):
        raise ReviewExchangeError("invalid_machine_identity")
    if (not isinstance(header.machine_media_type, str)
            or not re.fullmatch(r"[a-z]+/[a-z0-9][a-z0-9.+-]{0,100}", header.machine_media_type)):
        raise ReviewExchangeError("invalid_machine_identity")
    if (not isinstance(header.format_id, str) or not _SLUG.fullmatch(header.format_id)
            or not isinstance(header.format_version, str) or not _FORMAT_VERSION.fullmatch(header.format_version)
            or type(header.format_contract_version) is not int or header.format_contract_version < 1
            or not isinstance(header.adapter_namespace, str) or not _SLUG.fullmatch(header.adapter_namespace)
            or type(header.adapter_version) is not int
            or header.adapter_version < 1 or type(header.segmenter_version) is not int or header.segmenter_version < 1):
        raise ReviewExchangeError("invalid_format_identity")


def _validate_row(row: ReviewPackageRow, row_index: int) -> None:
    def fail(code: str) -> None:
        raise ReviewExchangeError(code, row_index=row_index)

    if (not isinstance(row.document_ref, str) or not row.document_ref.startswith("doc:v1:")
            or not re.fullmatch(r"doc:v1:[0-9a-f]{64}", row.document_ref)):
        fail("invalid_document_identity")
    if not isinstance(row.document_revision_id, str) or not re.fullmatch(r"rev:v1:[0-9a-f]{64}", row.document_revision_id):
        fail("invalid_document_revision")
    if (not isinstance(row.locator_schema, str) or not _SLUG.fullmatch(row.locator_schema)
            or type(row.locator_schema_version) is not int or row.locator_schema_version < 1):
        fail("invalid_locator_identity")
    if (not isinstance(row.locator_fingerprint, str) or not _DIGEST.fullmatch(row.locator_fingerprint)
            or not isinstance(row.unit_id, str) or not re.fullmatch(r"unit:v1:[0-9a-f]{64}", row.unit_id)):
        fail("invalid_unit_identity")
    if not isinstance(row.source_digest, str) or not re.fullmatch(r"scd:v1:[0-9a-f]{64}", row.source_digest):
        fail("invalid_source_fingerprint")
    if (not isinstance(row.source_text_fingerprint, str) or not _DIGEST.fullmatch(row.source_text_fingerprint)
            or not isinstance(row.machine_fingerprint, str) or not _DIGEST.fullmatch(row.machine_fingerprint)):
        fail("invalid_text_fingerprint")
    if not isinstance(row.review_status, str) or row.review_status not in {"pending", "reviewed", "rejected"}:
        fail("invalid_review_status")
    if row.reviewer_note is not None and (not isinstance(row.reviewer_note, str) or len(row.reviewer_note) > MAX_REVIEW_NOTE_CHARS):
        fail("invalid_reviewer_note")
    if type(row.protected_parts) is not tuple or len(row.protected_parts) > MAX_REVIEW_PARTS_PER_UNIT:
        fail("invalid_protected_contract")
    markers: set[str] = set()
    for part in row.protected_parts:
        if (type(part) is not tuple or len(part) != 2 or not isinstance(part[0], str)
                or not isinstance(part[1], str) or part[1] not in {"markup", "placeholder", "url", "identifier", "formula"}
                or _PLACEHOLDER.fullmatch(part[0]) is None or part[0] in markers):
            fail("invalid_protected_contract")
        markers.add(part[0])
    for value in (row.source_text, row.machine_translation, row.reviewed_text):
        if not isinstance(value, str) or len(value) > MAX_REVIEW_CELL_CHARS:
            fail("cell_too_large")
        try:
            value.encode("utf-8")
        except UnicodeError:
            fail("invalid_text")
    for value in (row.source_text, row.machine_translation, row.reviewed_text):
        if _PLACEHOLDER_LIKE in _PLACEHOLDER.sub("", value):
            fail("malformed_placeholder")


def _metadata_object(header: ReviewPackageHeader) -> dict[str, Any]:
    return _header_fields(header)


def encode_review_package(package: ReviewPackage) -> bytes:
    """Encode a package in canonical UTF-8 CSV (CRLF records, fixed column order)."""
    _validate_package_shape(package, check_duplicates=True)
    ordered = sorted(package.rows, key=lambda row: (row.document_ref, row.unit_id))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, dialect="excel", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL,
                        doublequote=True, escapechar=None)
    metadata = canonical_json(_metadata_object(package.header))
    if len(metadata) > MAX_REVIEW_METADATA_CHARS:
        raise ReviewExchangeError("metadata_too_large")
    writer.writerow((REVIEW_CSV_PREAMBLE, str(REVIEW_PACKAGE_SCHEMA_VERSION), metadata))
    writer.writerow(REVIEW_CSV_COLUMNS)
    total = 0
    for index, row in enumerate(ordered):
        cells = _row_cells(row)
        safe_cells = [neutralize_formula_cell(value) for value in cells]
        for value in cells:
            if len(value) > MAX_REVIEW_CELL_CHARS:
                raise ReviewExchangeError("cell_too_large", row_index=index)
            total += len(value)
            if total > MAX_REVIEW_TOTAL_CHARS:
                raise ReviewExchangeError("decoded_content_too_large", row_index=index)
        writer.writerow(safe_cells)
    try:
        data = stream.getvalue().encode("utf-8")
    except UnicodeError:
        raise ReviewExchangeError("invalid_text") from None
    if len(data) > MAX_REVIEW_RAW_BYTES:
        raise ReviewExchangeError("file_too_large")
    return data


def write_review_package(path: str | Path, package: ReviewPackage, *, overwrite: bool = False) -> Path:
    """Write only when explicitly requested; errors never echo the destination path."""
    destination = Path(path)
    data = encode_review_package(package)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb" if overwrite else "xb") as output:
            output.write(data)
    except FileExistsError:
        raise ReviewExchangeError("review_file_exists") from None
    except OSError:
        raise ReviewExchangeError("review_file_unwritable") from None
    return destination


def read_review_package(path: str | Path) -> ReviewPackage:
    """Read at most the raw-byte budget before decoding or parsing the file."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_REVIEW_RAW_BYTES + 1)
    except FileNotFoundError:
        raise ReviewExchangeError("review_file_not_found") from None
    except OSError:
        raise ReviewExchangeError("review_file_unreadable") from None
    return decode_review_package(raw)


def _no_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _metadata_cardinality(value: object, depth: int = 1) -> tuple[int, int]:
    if depth > MAX_REVIEW_METADATA_DEPTH:
        raise ReviewExchangeError("metadata_too_deep")
    if isinstance(value, dict):
        total = len(value)
        deepest = depth
        for child in value.values():
            count, nested_depth = _metadata_cardinality(child, depth + 1)
            total += count
            deepest = max(deepest, nested_depth)
        return total, deepest
    if isinstance(value, list):
        total = len(value)
        deepest = depth
        for child in value:
            count, nested_depth = _metadata_cardinality(child, depth + 1)
            total += count
            deepest = max(deepest, nested_depth)
        return total, deepest
    return 0, depth


def _header_from_metadata(value: object) -> ReviewPackageHeader:
    if type(value) is not dict or set(value) != {"adapter", "codec_version", "format", "machine_artifact", "package_id", "project", "schema_version", "source"}:
        raise ReviewExchangeError("invalid_metadata")
    adapter, form, artifact, project, source = (value.get(key) for key in ("adapter", "format", "machine_artifact", "project", "source"))
    if any(type(item) is not dict for item in (adapter, form, artifact, project, source)):
        raise ReviewExchangeError("invalid_metadata")
    if set(adapter) != {"namespace", "adapter_version", "segmenter_version"}:
        raise ReviewExchangeError("invalid_metadata")
    if set(form) != {"contract_version", "id", "version"}:
        raise ReviewExchangeError("invalid_metadata")
    if set(artifact) != {"artifact_id", "digest", "input_fingerprint", "media_type", "revision", "role", "schema_version", "size", "stage", "stage_version"}:
        raise ReviewExchangeError("invalid_metadata")
    if set(project) != {"id", "revision"} or set(source) != {"sha256", "size"}:
        raise ReviewExchangeError("invalid_metadata")
    try:
        header = ReviewPackageHeader(
            schema_version=value["schema_version"], codec_version=value["codec_version"], package_id=value["package_id"],
            project_id=project["id"], project_revision=project["revision"], source_sha256=source["sha256"], source_size=source["size"],
            machine_artifact_id=artifact["artifact_id"], machine_artifact_schema_version=artifact["schema_version"],
            machine_artifact_revision=artifact["revision"], machine_artifact_digest=artifact["digest"], machine_artifact_size=artifact["size"],
            machine_role=artifact["role"], machine_stage=artifact["stage"], machine_stage_version=artifact["stage_version"],
            machine_input_fingerprint=artifact["input_fingerprint"], machine_media_type=artifact["media_type"],
            format_id=form["id"], format_version=form["version"], format_contract_version=form["contract_version"],
            adapter_namespace=adapter["namespace"], adapter_version=adapter["adapter_version"], segmenter_version=adapter["segmenter_version"],
        )
    except (KeyError, TypeError):
        raise ReviewExchangeError("invalid_metadata") from None
    _validate_header(header)
    return header


def _decimal(value: str, *, allow_zero: bool = False) -> int:
    pattern = r"(?:0|[1-9][0-9]*)" if allow_zero else r"[1-9][0-9]*"
    if not re.fullmatch(pattern, value):
        raise ReviewExchangeError("invalid_integer")
    result = int(value)
    if result >= 2**63:
        raise ReviewExchangeError("invalid_integer")
    return result


def _validate_csv_newlines(text: str, *, max_columns: int = MAX_REVIEW_COLUMNS, delimiter: str = ",") -> None:
    """Bound record cardinality before csv.reader can allocate per-cell objects."""
    quoted = False
    field_start = True
    field_count = 1
    index = 0
    while index < len(text):
        character = text[index]
        if quoted:
            if character == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    index += 2
                    continue
                quoted = False
                field_start = False
        elif character == '"' and field_start:
            quoted = True
            field_start = False
        elif character == delimiter:
            field_start = True
            field_count += 1
            if field_count > max_columns:
                raise ReviewExchangeError("invalid_column_count")
        elif character == "\r":
            if index + 1 >= len(text) or text[index + 1] != "\n":
                raise ReviewExchangeError("invalid_newline")
            field_start = True
            field_count = 1
            index += 2
            continue
        elif character == "\n":
            field_start = True
            field_count = 1
        else:
            field_start = False
        index += 1


def _parse_protected(value: str, row_index: int) -> tuple[tuple[str, str], ...]:
    if len(value) > MAX_REVIEW_METADATA_CHARS:
        raise ReviewExchangeError("metadata_too_large", row_index=row_index)
    try:
        data = json.loads(value, object_pairs_hook=_no_duplicate_json_pairs)
    except (ValueError, RecursionError):
        raise ReviewExchangeError("invalid_protected_contract", row_index=row_index) from None
    if not isinstance(data, list) or len(data) > MAX_REVIEW_PARTS_PER_UNIT:
        raise ReviewExchangeError("invalid_protected_contract", row_index=row_index)
    if canonical_json(data) != value:
        raise ReviewExchangeError("noncanonical_protected_contract", row_index=row_index)
    result = []
    for entry in data:
        if type(entry) is not dict or set(entry) != {"kind", "placeholder"}:
            raise ReviewExchangeError("invalid_protected_contract", row_index=row_index)
        result.append((entry["placeholder"], entry["kind"]))
    return tuple(result)


def _parse_row(cells: list[str], row_index: int) -> ReviewPackageRow:
    if len(cells) != MAX_REVIEW_COLUMNS:
        raise ReviewExchangeError("invalid_column_count", row_index=row_index)
    decoded = [restore_formula_cell(cell) for cell in cells]
    if any(len(value) > MAX_REVIEW_CELL_CHARS for value in decoded):
        raise ReviewExchangeError("cell_too_large", row_index=row_index)
    note = _decode_nullable_note(decoded[14])
    row = ReviewPackageRow(
        document_ref=decoded[0], document_revision_id=decoded[1], locator_schema=decoded[2],
        locator_schema_version=_decimal(decoded[3]), locator_fingerprint=decoded[4], unit_id=decoded[5],
        source_digest=decoded[6], source_text_fingerprint=decoded[7], source_text=decoded[8],
        machine_translation=decoded[9], machine_fingerprint=decoded[10], reviewed_text=decoded[11],
        protected_parts=_parse_protected(decoded[12], row_index), review_status=decoded[13], reviewer_note=note,
    )
    _validate_row(row, row_index)
    return row


def decode_review_package(raw: bytes) -> ReviewPackage:
    """Decode bounded UTF-8 CSV; row ordering is irrelevant to identity validation.

    A spreadsheet that saves this file may add a byte order mark, pad the shorter first
    record with empty cells, or use the semicolon of its locale. None of that changes a
    value, so all of it is accepted. Encoding again gives the canonical form.
    """
    if type(raw) is not bytes:
        raise ReviewExchangeError("invalid_input")
    if len(raw) > MAX_REVIEW_RAW_BYTES:
        raise ReviewExchangeError("file_too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ReviewExchangeError("invalid_encoding") from None
    try:
        delimiter = ";" if _SEMICOLON_PREAMBLE.match(text) else ","
        _validate_csv_newlines(text, delimiter=delimiter)
        reader = csv.reader(io.StringIO(text, newline=""), dialect="excel", delimiter=delimiter, strict=True)
        preamble = next(reader, None)
        if preamble is not None and not any(preamble[3:]):
            preamble = preamble[:3]
        if preamble is None or len(preamble) != 3 or preamble[0] != REVIEW_CSV_PREAMBLE:
            raise ReviewExchangeError("invalid_preamble")
        if preamble[1] != str(REVIEW_PACKAGE_SCHEMA_VERSION):
            raise ReviewExchangeError("unsupported_schema_version")
        metadata_text = preamble[2]
        if len(metadata_text) > MAX_REVIEW_METADATA_CHARS:
            raise ReviewExchangeError("metadata_too_large")
        try:
            metadata = json.loads(metadata_text, object_pairs_hook=_no_duplicate_json_pairs)
        except (ValueError, RecursionError):
            raise ReviewExchangeError("invalid_metadata") from None
        if canonical_json(metadata) != metadata_text:
            raise ReviewExchangeError("noncanonical_metadata")
        item_count, depth = _metadata_cardinality(metadata)
        if item_count > MAX_REVIEW_METADATA_ITEMS:
            raise ReviewExchangeError("metadata_too_many_items")
        if depth > MAX_REVIEW_METADATA_DEPTH:
            raise ReviewExchangeError("metadata_too_deep")
        header = _header_from_metadata(metadata)
        header_row = next(reader, None)
        if header_row != list(REVIEW_CSV_COLUMNS):
            raise ReviewExchangeError("incompatible_columns")
        rows: list[ReviewPackageRow] = []
        total = 0
        for row_index, cells in enumerate(reader):
            if row_index >= MAX_REVIEW_ROWS:
                raise ReviewExchangeError("too_many_rows")
            if not any(cells):
                continue
            if len(cells) != MAX_REVIEW_COLUMNS:
                raise ReviewExchangeError("invalid_column_count", row_index=row_index)
            for cell in cells:
                # Include headroom for the fixed reversible formula escape added by export.
                if len(cell) > MAX_REVIEW_CELL_CHARS + 128:
                    raise ReviewExchangeError("cell_too_large", row_index=row_index)
                total += len(restore_formula_cell(cell))
                if total > MAX_REVIEW_TOTAL_CHARS:
                    raise ReviewExchangeError("decoded_content_too_large", row_index=row_index)
            rows.append(_parse_row(cells, row_index))
    except ReviewExchangeError:
        raise
    except (csv.Error, StopIteration, UnicodeError, ValueError, RecursionError):
        raise ReviewExchangeError("malformed_csv") from None
    package = ReviewPackage(header, tuple(rows))
    _validate_package_shape(package, check_duplicates=True)
    return package


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
        if row.review_status != "reviewed":
            raise ReviewExchangeError("review_not_approved")
        if not row.reviewed_text:
            raise ReviewExchangeError("empty_review_text")
        markers = [marker for marker, _kind in expected_parts]
        _validate_review_placeholders(row.reviewed_text, markers)
        mapped = _to_local_tokens(row.reviewed_text, markers)
        _validate_local_tokens_for_unit(mapped, unit)
        staged.append(UnitTranslation(unit.unit_id, mapped))

    if len(staged) != len(expected_by_key):
        raise ReviewExchangeError("incomplete_package")
    doc_by_id = {document.document_id: document for document in documents}
    ordered_documents = tuple(doc_by_id[key] for key in sorted(doc_by_id))
    return ReviewReconstructionPlan(ordered_documents, tuple(staged))


def _validate_review_placeholders(text: str, expected: Sequence[str]) -> None:
    matches = list(_PLACEHOLDER.finditer(text))
    pieces = [match.group(0) for match in matches]
    if _PLACEHOLDER_LIKE in _PLACEHOLDER.sub("", text):
        raise ReviewExchangeError("malformed_placeholder")
    if len(pieces) != len(set(pieces)):
        raise ReviewExchangeError("duplicate_placeholder")
    unknown = set(pieces) - set(expected)
    if unknown:
        raise ReviewExchangeError("foreign_placeholder")
    missing = set(expected) - set(pieces)
    if missing:
        raise ReviewExchangeError("missing_placeholder")
    if pieces != list(expected):
        raise ReviewExchangeError("invalid_placeholder_order")


def _to_local_tokens(text: str, expected: Sequence[str]) -> str:
    lookup = {marker: f"__AYVU_TAG_{index}__" for index, marker in enumerate(expected)}
    return _PLACEHOLDER.sub(lambda match: lookup[match.group(0)], text)


def _validate_local_tokens_for_unit(text: str, unit: TranslationUnit) -> None:
    found = [match.group(0) for match in _LOCAL_TOKEN.finditer(text)]
    expected = [f"__AYVU_TAG_{index}__" for index in range(len(unit.content.protected_parts))]
    if found != expected:
        if len(found) != len(set(found)):
            raise ReviewExchangeError("duplicate_placeholder")
        if any(item not in expected for item in found):
            raise ReviewExchangeError("unknown_placeholder")
        if len(found) != len(expected):
            raise ReviewExchangeError("missing_placeholder")
        raise ReviewExchangeError("invalid_placeholder_order")
    if INTERNAL_MARKER_PATTERN.search(_LOCAL_TOKEN.sub("", text)):
        raise ReviewExchangeError("malformed_placeholder")
    # XML-forbidden code points cannot enter structural reconstruction.
    if re.search(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", _LOCAL_TOKEN.sub("", text)):
        raise ReviewExchangeError("invalid_text")

