from __future__ import annotations


import pytest

from ayvu.review.exchange_mapping import map_review_package
from ayvu.review.csv_export import ReviewSegment, write_review_csv
from ayvu.review.csv_import import LegacyReviewInspection, ReviewImportData, ReviewImportError, ReviewRow, read_review_csv

from tests.review.review_exchange_support import error_code


def test_legacy_v1_is_boundedly_inspectable_and_redacted(tmp_path, monkeypatch):
    """Applying one is refused by the review application: tests/review/test_review_application_apply.py."""
    source_canary = "BOOK_TEXT_CANARY_148"
    path_canary = "PRIVATE_PATH_CANARY_148"
    segment = ReviewSegment(
        "c0001-s0001", "private.epub", "output.epub", 1, "chapter", "chapter.xhtml", path_canary,
        "text", "en", "pt", source_canary, "REVIEW_TEXT_CANARY_148")
    csv_path = write_review_csv(tmp_path / "legacy.csv", [segment])
    inspection = read_review_csv(csv_path)

    assert isinstance(inspection, LegacyReviewInspection)
    assert source_canary not in repr(inspection)
    assert source_canary not in repr(inspection.rows[0])
    assert path_canary not in repr(segment)
    monkeypatch.setattr("ayvu.review.csv_import.MAX_REVIEW_BYTES", 3)
    with pytest.raises(ReviewImportError) as oversized:
        read_review_csv(csv_path)
    assert oversized.value.code == "review_file_too_large"
    assert path_canary not in str(oversized.value)


def test_v1_positional_text_matches_never_become_v2_identity():
    legacy = ReviewImportData((ReviewRow("c0001-s0001", "chapter.xhtml", "same text", "same text"),), ())
    error_code("invalid_package", lambda: map_review_package(
        legacy, project=None, machine_artifact=None, descriptor=None, format_version="3.0",
        documents=(), machine_translations={}))
