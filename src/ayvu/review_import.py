from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


class ReviewImportError(Exception):
    """A bounded v1 inspection error with no path, row content, or parser detail."""

    def __init__(self, code: str) -> None:
        safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "review_import_failed"
        self.code = safe_code
        super().__init__(safe_code)


# Columns the import needs to locate and apply a reviewed segment. Other export
# columns (chapter index/name, languages, from_cache) are optional here so that
# hand-edited review files stay usable as long as these are present.
REQUIRED_REVIEW_COLUMNS = ("segment_id", "document_path", "original", "translated")


@dataclass(frozen=True, repr=False)
class ReviewRow:
    segment_id: str
    document_path: str
    original: str
    translated: str
    segment_kind: str = ""
    source_language: str = ""
    target_language: str = ""

    def __repr__(self) -> str:
        return "ReviewRow(<sensitive legacy fields redacted>)"


@dataclass(frozen=True, repr=False)
class LegacyReviewInspection:
    rows: tuple[ReviewRow, ...]
    duplicate_ids: tuple[str, ...]
    warnings: tuple[str, ...] = field(default=())

    @property
    def source_language(self) -> str:
        return next((row.source_language for row in self.rows if row.source_language), "")

    @property
    def target_language(self) -> str:
        return next((row.target_language for row in self.rows if row.target_language), "")

    def __repr__(self) -> str:
        return f"LegacyReviewInspection(rows={len(self.rows)}, warnings={len(self.warnings)})"


# Keep the old type name for callers that construct v1 inspection values. It is
# deliberately not an apply-capable object.
ReviewImportData = LegacyReviewInspection


MAX_REVIEW_BYTES = 32 * 1024 * 1024
MAX_REVIEW_ROWS = 100_000
MAX_REVIEW_COLUMNS = 64
MAX_REVIEW_CELL_CHARS = 64 * 1024
MAX_REVIEW_TOTAL_CHARS = 16 * 1024 * 1024


def read_review_csv(path: str | Path) -> LegacyReviewInspection:
    """Read only the bounded positional v1 schema for diagnosis; never assigns identity."""
    source = Path(path)
    try:
        with source.open("rb") as input_file:
            raw = input_file.read(MAX_REVIEW_BYTES + 1)
    except FileNotFoundError:
        raise ReviewImportError("review_file_not_found") from None
    except OSError:
        raise ReviewImportError("review_file_unreadable") from None
    return read_review_csv_bytes(raw)


def read_review_csv_bytes(raw: bytes) -> LegacyReviewInspection:
    """Inspect bounded v1 bytes without a filesystem write or path-backed temporary file."""
    if type(raw) is not bytes:
        raise ReviewImportError("invalid_input")
    if len(raw) > MAX_REVIEW_BYTES:
        raise ReviewImportError("review_file_too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReviewImportError("unsupported_encoding")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ReviewImportError("invalid_encoding") from None

    try:
        _validate_csv_column_budget(text)
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        fieldnames = next(reader, None)
        _check_columns(fieldnames)
        assert fieldnames is not None
        if len(fieldnames) > MAX_REVIEW_COLUMNS or len(set(fieldnames)) != len(fieldnames):
            raise ReviewImportError("invalid_columns")
        rows: list[ReviewRow] = []
        total = 0
        for index, values in enumerate(reader):
            if index >= MAX_REVIEW_ROWS:
                raise ReviewImportError("too_many_rows")
            if len(values) != len(fieldnames):
                raise ReviewImportError("invalid_column_count")
            if any(len(value) > MAX_REVIEW_CELL_CHARS for value in values):
                raise ReviewImportError("cell_too_large")
            total += sum(map(len, values))
            if total > MAX_REVIEW_TOTAL_CHARS:
                raise ReviewImportError("decoded_content_too_large")
            record = dict(zip(fieldnames, values))
            rows.append(_row_from_record(record))
    except ReviewImportError:
        raise
    except (csv.Error, UnicodeError, ValueError, RecursionError):
        raise ReviewImportError("malformed_csv") from None

    result = tuple(rows)
    duplicates = _duplicate_ids(result)
    warnings = ("duplicate_segment_ids",) if duplicates else ()
    return LegacyReviewInspection(rows=result, duplicate_ids=duplicates, warnings=warnings)


def _check_columns(fieldnames: list[str] | None) -> None:
    if not fieldnames:
        raise ReviewImportError("missing_header")
    missing = [column for column in REQUIRED_REVIEW_COLUMNS if column not in fieldnames]
    if missing:
        raise ReviewImportError("missing_required_columns")


def _validate_csv_column_budget(text: str) -> None:
    """Reject over-wide records before csv.reader allocates one object per cell."""
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
        elif character == ",":
            field_start = True
            field_count += 1
            if field_count > MAX_REVIEW_COLUMNS:
                raise ReviewImportError("invalid_column_count")
        elif character == "\r":
            field_start = True
            field_count = 1
            if index + 1 < len(text) and text[index + 1] == "\n":
                index += 2
                continue
        elif character == "\n":
            field_start = True
            field_count = 1
        else:
            field_start = False
        index += 1


def _row_from_record(record: dict[str, str]) -> ReviewRow:
    return ReviewRow(
        segment_id=(record.get("segment_id") or "").strip(),
        document_path=(record.get("document_path") or "").strip(),
        original=record.get("original") or "",
        translated=record.get("translated") or "",
        segment_kind=(record.get("segment_kind") or "").strip(),
        source_language=(record.get("source_language") or "").strip(),
        target_language=(record.get("target_language") or "").strip(),
    )


def _duplicate_ids(rows: tuple[ReviewRow, ...]) -> tuple[str, ...]:
    counts = Counter(row.segment_id for row in rows if row.segment_id)
    return tuple(sorted(segment_id for segment_id, count in counts.items() if count > 1))
