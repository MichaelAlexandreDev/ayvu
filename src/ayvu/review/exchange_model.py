from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..content import ContentDocument, TranslationUnit
from ..formats import UnitTranslation


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
