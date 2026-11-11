from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


METADATA_SEGMENT_ID = "metadata-title"
METADATA_SEGMENT_KIND = "metadata_title"
METADATA_CHAPTER_NAME = "metadata"
_SEGMENT_ID_PATTERN = re.compile(r"^c(\d+)-s(\d+)$")


@dataclass(frozen=True)
class SegmentLocation:
    is_metadata: bool
    chapter_index: int | None = None
    segment_index: int | None = None


def format_segment_id(chapter_index: int, segment_index: int) -> str:
    return f"c{chapter_index:04d}-s{segment_index:04d}"


def parse_segment_id(segment_id: str) -> SegmentLocation | None:
    value = segment_id.strip()
    if value == METADATA_SEGMENT_ID:
        return SegmentLocation(is_metadata=True)
    match = _SEGMENT_ID_PATTERN.match(value)
    if match is None:
        return None
    return SegmentLocation(
        is_metadata=False,
        chapter_index=int(match.group(1)),
        segment_index=int(match.group(2)),
    )


REVIEW_CSV_COLUMNS = (
    "segment_id",
    "source_epub",
    "output_epub",
    "chapter_index",
    "chapter_name",
    "document_name",
    "document_path",
    "segment_kind",
    "source_language",
    "target_language",
    "from_cache",
    "original",
    "translated",
)


@dataclass(frozen=True, repr=False)
class ReviewSegment:
    segment_id: str
    source_epub: str
    output_epub: str
    chapter_index: int
    chapter_name: str
    document_name: str
    document_path: str
    segment_kind: str
    source_language: str
    target_language: str
    original: str
    translated: str
    from_cache: bool = False

    def __repr__(self) -> str:
        return "ReviewSegment(<sensitive review fields redacted>)"


def write_review_csv(
    path: str | Path,
    segments: Iterable[ReviewSegment],
    overwrite: bool = False,
) -> Path:
    destination = Path(path)
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=REVIEW_CSV_COLUMNS)
        writer.writeheader()
        for segment in segments:
            writer.writerow(_segment_row(segment))

    return destination


def _segment_row(segment: ReviewSegment) -> dict[str, object]:
    return {
        "segment_id": segment.segment_id,
        "source_epub": segment.source_epub,
        "output_epub": segment.output_epub,
        "chapter_index": segment.chapter_index,
        "chapter_name": segment.chapter_name,
        "document_name": segment.document_name,
        "document_path": segment.document_path,
        "segment_kind": segment.segment_kind,
        "source_language": segment.source_language,
        "target_language": segment.target_language,
        "from_cache": "true" if segment.from_cache else "false",
        "original": segment.original,
        "translated": segment.translated,
    }
