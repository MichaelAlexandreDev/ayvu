import csv
from pathlib import Path

import pytest

from ayvu.review_export import REVIEW_CSV_COLUMNS, ReviewSegment, write_review_csv
from ayvu.review_import import ReviewImportError, read_review_csv


def _write_csv(path: Path, rows: list[dict[str, object]], columns=REVIEW_CSV_COLUMNS) -> Path:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def test_read_review_csv_reads_rows_and_languages(tmp_path: Path):
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
        translated="Ola, leitor.",
    )
    csv_path = write_review_csv(tmp_path / "review.csv", [segment])

    data = read_review_csv(csv_path)

    assert len(data.rows) == 1
    row = data.rows[0]
    assert row.segment_id == "c0001-s0002"
    assert row.document_path == "OEBPS/text/chapter1.xhtml"
    assert row.original == "Hello, reader."
    assert row.translated == "Ola, leitor."
    assert row.segment_kind == "text"
    assert data.source_language == "en"
    assert data.target_language == "pt"
    assert data.duplicate_ids == ()


def test_read_review_csv_detects_duplicate_segment_ids(tmp_path: Path):
    rows = [
        {**_base_row(), "segment_id": "c0001-s0001"},
        {**_base_row(), "segment_id": "c0001-s0001"},
        {**_base_row(), "segment_id": "c0001-s0002"},
    ]
    csv_path = _write_csv(tmp_path / "review.csv", rows)

    data = read_review_csv(csv_path)

    assert data.duplicate_ids == ("c0001-s0001",)
    assert len(data.rows) == 3


def test_read_review_csv_rejects_missing_required_columns(tmp_path: Path):
    csv_path = tmp_path / "broken.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=["segment_id", "translated"])
        writer.writeheader()
        writer.writerow({"segment_id": "c0001-s0001", "translated": "x"})

    with pytest.raises(ReviewImportError) as error:
        read_review_csv(csv_path)

    assert error.value.code == "missing_required_columns"


def test_read_review_csv_rejects_missing_file(tmp_path: Path):
    with pytest.raises(ReviewImportError):
        read_review_csv(tmp_path / "missing.csv")


def test_legacy_column_cardinality_fails_before_csv_materializes_cells(tmp_path: Path, monkeypatch):
    import ayvu.review_import as importer

    path = tmp_path / "too-many-columns.csv"
    path.write_bytes(b"x," * (importer.MAX_REVIEW_COLUMNS * 1000))
    monkeypatch.setattr(importer.csv, "reader", lambda *_args, **_kwargs: pytest.fail("csv.reader must not run"))

    with pytest.raises(ReviewImportError) as error:
        read_review_csv(path)

    assert error.value.code == "invalid_column_count"


@pytest.mark.parametrize(
    "limit_name,value,code",
    [
        ("MAX_REVIEW_ROWS", 0, "too_many_rows"),
        ("MAX_REVIEW_COLUMNS", 1, "invalid_column_count"),
        ("MAX_REVIEW_CELL_CHARS", 1, "cell_too_large"),
        ("MAX_REVIEW_TOTAL_CHARS", 1, "decoded_content_too_large"),
    ],
)
def test_legacy_reader_enforces_each_decoded_budget(tmp_path: Path, monkeypatch, limit_name, value, code):
    import ayvu.review_import as importer

    path = write_review_csv(tmp_path / "legacy.csv", [
        ReviewSegment("c0001-s0001", "book.epub", "out.epub", 1, "chapter", "chapter.xhtml",
                      "OEBPS/chapter.xhtml", "text", "en", "pt", "Source text", "Machine text")
    ])
    monkeypatch.setattr(importer, limit_name, value)

    with pytest.raises(ReviewImportError) as error:
        read_review_csv(path)

    assert error.value.code == code


def _base_row() -> dict[str, object]:
    return {
        "segment_id": "c0001-s0001",
        "source_epub": "book.epub",
        "output_epub": "book-pt.epub",
        "chapter_index": 1,
        "chapter_name": "text/chapter1.xhtml",
        "document_name": "text/chapter1.xhtml",
        "document_path": "OEBPS/text/chapter1.xhtml",
        "segment_kind": "text",
        "source_language": "en",
        "target_language": "pt",
        "from_cache": "false",
        "original": "Hello.",
        "translated": "Ola.",
    }
