from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from .exchange_model import (
    MAX_REVIEW_CELL_CHARS,
    MAX_REVIEW_COLUMNS,
    MAX_REVIEW_METADATA_CHARS,
    MAX_REVIEW_METADATA_DEPTH,
    MAX_REVIEW_METADATA_ITEMS,
    MAX_REVIEW_PARTS_PER_UNIT,
    MAX_REVIEW_RAW_BYTES,
    MAX_REVIEW_ROWS,
    MAX_REVIEW_TOTAL_CHARS,
    REVIEW_CSV_COLUMNS,
    REVIEW_CSV_PREAMBLE,
    REVIEW_PACKAGE_SCHEMA_VERSION,
    ReviewExchangeError,
    ReviewPackage,
    ReviewPackageHeader,
    ReviewPackageRow,
    _header_fields,
    _validate_header,
    _validate_package_shape,
    _validate_row,
    canonical_json,
)


_FORMULA_ESCAPE_START = "'⟪AYVU-FORMULA-ESCAPE-v1⟫"


_SEMICOLON_PREAMBLE = re.compile(r'"?' + REVIEW_CSV_PREAMBLE + r'"?;')


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
