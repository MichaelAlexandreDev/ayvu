from pathlib import Path

import pytest

from ayvu.v1.epub import apply_reviewed_epub
from ayvu.review.csv_import import ReviewImportData, ReviewImportError, ReviewRow, read_review_csv

from tests.support.epub_io_support import _export_review_csv
from tests.support.epub_io_support import _rewrite_csv


def test_apply_reviewed_epub_rejects_a_valid_v1_inline_review_before_writing(minimal_epub_path: Path, tmp_path: Path):
    csv_path = _export_review_csv(minimal_epub_path, tmp_path)
    source_before = minimal_epub_path.read_bytes()
    output_path = tmp_path / "minimal-reviewed.epub"

    with pytest.raises(ReviewImportError, match="^legacy_reexport_required$"):
        apply_reviewed_epub(minimal_epub_path, output_path, read_review_csv(csv_path))

    assert minimal_epub_path.read_bytes() == source_before
    assert not output_path.exists()


def test_apply_reviewed_epub_rejects_edited_v1_even_when_text_is_positionally_matched(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    csv_path = _export_review_csv(minimal_epub_path, tmp_path)

    def tamper_paragraph(row: dict) -> None:
        if "Hello reader" in row["original"]:
            row["original"] = "Different source text."
            row["translated"] = "Nao deve ser aplicado."

    _rewrite_csv(csv_path, tamper_paragraph)

    output_path = tmp_path / "minimal-reviewed.epub"
    with pytest.raises(ReviewImportError, match="^legacy_reexport_required$"):
        apply_reviewed_epub(minimal_epub_path, output_path, read_review_csv(csv_path))
    assert not output_path.exists()


def test_apply_reviewed_epub_rejects_v1_unknown_document(minimal_epub_path: Path, tmp_path: Path):
    review = ReviewImportData(
        rows=(
            ReviewRow(
                segment_id="c0001-s0001",
                document_path="OEBPS/text/ghost.xhtml",
                original="Whatever",
                translated="Tanto faz",
            ),
        ),
        duplicate_ids=(),
    )

    output_path = tmp_path / "minimal-reviewed.epub"
    with pytest.raises(ReviewImportError, match="^legacy_reexport_required$"):
        apply_reviewed_epub(minimal_epub_path, output_path, review)
    assert not output_path.exists()


def test_apply_reviewed_epub_rejects_v1_out_of_range_segment_index(minimal_epub_path: Path, tmp_path: Path):
    review = ReviewImportData(
        rows=(
            ReviewRow(
                segment_id="c0001-s9999",
                document_path="OEBPS/text/chapter1.xhtml",
                original="Out of range",
                translated="Fora de alcance",
            ),
        ),
        duplicate_ids=(),
    )

    output_path = tmp_path / "minimal-reviewed.epub"
    with pytest.raises(ReviewImportError, match="^legacy_reexport_required$"):
        apply_reviewed_epub(minimal_epub_path, output_path, review)
    assert not output_path.exists()
