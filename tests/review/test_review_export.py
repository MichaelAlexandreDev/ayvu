import csv
from pathlib import Path

import pytest

from ayvu.review.csv_export import REVIEW_CSV_COLUMNS, ReviewSegment, write_review_csv


def test_write_review_csv_writes_segments_with_stable_columns(tmp_path: Path):
    output = tmp_path / "review.csv"
    segment = ReviewSegment(
        segment_id="c0001-s0002",
        source_epub="book.epub",
        output_epub="book-pt.epub",
        chapter_index=1,
        chapter_name="text/chapter1.xhtml",
        document_name="text/chapter1.xhtml",
        document_path="OEBPS/text/chapter1.xhtml",
        segment_kind="text",
        source_language="en",
        target_language="pt",
        original="Hello, reader.",
        translated="Ola, leitor.\nTudo bem?",
        from_cache=True,
    )

    written = write_review_csv(output, [segment])

    with written.open(encoding="utf-8", newline="") as input_file:
        rows = list(csv.DictReader(input_file))

    assert written == output
    assert list(rows[0]) == list(REVIEW_CSV_COLUMNS)
    assert rows == [
        {
            "segment_id": "c0001-s0002",
            "source_epub": "book.epub",
            "output_epub": "book-pt.epub",
            "chapter_index": "1",
            "chapter_name": "text/chapter1.xhtml",
            "document_name": "text/chapter1.xhtml",
            "document_path": "OEBPS/text/chapter1.xhtml",
            "segment_kind": "text",
            "source_language": "en",
            "target_language": "pt",
            "from_cache": "true",
            "original": "Hello, reader.",
            "translated": "Ola, leitor.\nTudo bem?",
        }
    ]


def test_write_review_csv_does_not_overwrite_by_default(tmp_path: Path):
    output = tmp_path / "review.csv"
    output.write_text("already here", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_review_csv(output, [])

    assert output.read_text(encoding="utf-8") == "already here"
