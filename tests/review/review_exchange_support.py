from __future__ import annotations
import csv
import hashlib
import io
import json
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile
import pytest
from bs4 import BeautifulSoup
from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.content.model import UNKNOWN_LOCALE
from ayvu.formats.epub.adapter import EPUB_ADAPTER, EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import ContentScope, ExtractionRequest, UnitTranslation
from ayvu.formats.service import FormatRegistry, FormatService
from ayvu.formats.epub.html_structure import extract_html_document, html_unit_template, reconstruct_html
from ayvu.projects.model import ProjectManifest, ProjectRequest
from ayvu.review import exchange_codec, exchange_files, exchange_model
from ayvu.review.exchange_model import (
    MAX_REVIEW_CELL_CHARS,
    MAX_REVIEW_COLUMNS,
    MAX_REVIEW_DOCUMENTS,
    MAX_REVIEW_METADATA_CHARS,
    MAX_REVIEW_METADATA_DEPTH,
    MAX_REVIEW_METADATA_ITEMS,
    MAX_REVIEW_NOTE_CHARS,
    MAX_REVIEW_PARTS_PER_UNIT,
    MAX_REVIEW_RAW_BYTES,
    MAX_REVIEW_ROWS,
    MAX_REVIEW_TOTAL_CHARS,
    REVIEW_CSV_COLUMNS,
    REVIEW_CSV_PREAMBLE,
    ReviewExchangeError,
    ReviewPackage,
    ReviewPackageRow,
    canonical_json,
)
from ayvu.review.exchange_codec import decode_review_package, encode_review_package, neutralize_formula_cell, restore_formula_cell
from ayvu.review.exchange import export_review_package
from ayvu.review.exchange_mapping import map_review_package
from ayvu.review.exchange_files import read_review_package, write_review_package
from ayvu.review.csv_export import ReviewSegment, write_review_csv
from ayvu.review.csv_import import LegacyReviewInspection, ReviewImportData, ReviewImportError, ReviewRow, read_review_csv
from tests.support.epub_fakes import epub_bytes

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


PROJECT_ID = "00000000-0000-4000-8000-000000000148"


JOB_ID = "00000000-0000-4000-8000-000000000149"


ARTIFACT_ID = "00000000-0000-4000-8000-000000000150"


OTHER_PROJECT_ID = "00000000-0000-4000-8000-000000000151"


OTHER_ARTIFACT_ID = "00000000-0000-4000-8000-000000000152"


DESCRIPTOR = epub_format_adapter().descriptor


def make_context(html: bytes | str = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hello <em>reader</em></p></body></html>',
                 *, logical_key: str = "OEBPS/text/chapter.xhtml", project_id: str = PROJECT_ID,
                 artifact_id: str = ARTIFACT_ID):
    source = html.encode("utf-8") if isinstance(html, str) else html
    source_sha = hashlib.sha256(source).hexdigest()
    project = ProjectManifest.create(
        ProjectRequest("/private/book.epub", len(source), source_sha, "en", "pt"), project_id)
    document = extract_html_document(
        source, project_id, logical_key, adapter=EPUB_ADAPTER, source_locale=UNKNOWN_LOCALE,
        source_fingerprint=f"sha256:{source_sha}")
    artifact_request = ArtifactRequest(
        project_id, JOB_ID, "raw_results", "translate", 1,
        "sha256:" + "e" * 64, "application/json")
    artifact = Artifact(
        artifact_id, artifact_request, ArtifactState.READY, 3, NOW, NOW,
        size=321, digest="sha256:" + "f" * 64)
    machine = {unit.unit_id: html_unit_template(unit) + " [machine]"
               for unit in document.units}
    package = export_review_package(
        project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=(document,), machine_translations=machine)
    return source, project, artifact, (document,), machine, package


def mark_reviewed(package: ReviewPackage, transform=lambda text: text):
    return replace(package, rows=tuple(
        replace(row, reviewed_text=transform(row.reviewed_text), review_status="reviewed")
        for row in package.rows))


def error_code(code: str, call):
    with pytest.raises(ReviewExchangeError) as caught:
        call()
    assert caught.value.code == code
    assert str(caught.value) == code
    return caught.value


def _csv_bytes(records):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(records)
    return stream.getvalue().encode("utf-8")


def _saved_by_a_spreadsheet(encoded: bytes, *, delimiter: str = ",", quoting: int = csv.QUOTE_MINIMAL,
                            terminator: str = "\n") -> bytes:
    """What a spreadsheet writes: every record padded to the widest one."""
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    width = max(len(record) for record in records)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, quoting=quoting, lineterminator=terminator)
    for record in records:
        writer.writerow(record + [""] * (width - len(record)))
    return output.getvalue().encode("utf-8")


class MemorySource:
    def __init__(self, content: bytes):
        self.content = content
        self.fingerprint_value = "sha256:" + hashlib.sha256(content).hexdigest()
        self.extension = ".epub"

    def size(self):
        return len(self.content)

    def fingerprint(self):
        return self.fingerprint_value

    def head(self, limit):
        return self.content[:limit]

    def open(self):
        return io.BytesIO(self.content)
