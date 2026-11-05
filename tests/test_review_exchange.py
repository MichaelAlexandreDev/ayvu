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

from ayvu.artifact import Artifact, ArtifactRequest, ArtifactState
from ayvu.content import UNKNOWN_LOCALE
from ayvu.epub_format import EPUB_ADAPTER, EPUB_REGISTRATION, epub_format_adapter
from ayvu.format_adapter import ContentScope, ExtractionRequest, UnitTranslation
from ayvu.format_service import FormatRegistry, FormatService
from ayvu.html_structure import extract_html_document, html_unit_template, reconstruct_html
from ayvu.project import ProjectManifest, ProjectRequest
from ayvu.review_exchange import (
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
    decode_review_package,
    encode_review_package,
    export_review_package,
    map_review_package,
    neutralize_formula_cell,
    read_review_package,
    restore_formula_cell,
    write_review_package,
)
from ayvu.review_export import ReviewSegment, write_review_csv
from ayvu.review_import import LegacyReviewInspection, ReviewImportData, ReviewImportError, ReviewRow, read_review_csv
from epub_fakes import epub_bytes


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


def test_v2_codec_has_independent_versions_fixed_order_and_canonical_utf8_newlines():
    _source, _project, _artifact, _documents, _machine, package = make_context(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Olá, "leitor"\ncomo vai?</p></body></html>')
    row = replace(package.rows[0], reviewed_text='“sim,\ncom aspas”', reviewer_note="nota com ç")
    package = replace(package, rows=(row,))

    encoded = encode_review_package(package)

    assert encoded == encode_review_package(package)
    assert encoded.startswith((REVIEW_CSV_PREAMBLE + ",2,").encode("ascii"))
    assert encoded.endswith(b"\r\n")
    assert b"/private/book.epub" not in encoded
    assert b"\ncom aspas" in encoded and "ç".encode() in encoded
    decoded = decode_review_package(encoded)
    assert decoded.header.schema_version == 2
    assert decoded.header.codec_version == 1
    assert decoded.rows[0].reviewed_text == '“sim,\ncom aspas”'
    assert encode_review_package(decoded) == encoded
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    assert records[1] == list(REVIEW_CSV_COLUMNS)


def test_row_order_is_independent_of_extraction_argument_order():
    source, project, artifact, documents, machine, _package = make_context(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>First</p></body></html>')
    source_fingerprint = "sha256:" + hashlib.sha256(source).hexdigest()
    second = extract_html_document(source, PROJECT_ID, "OEBPS/text/second.xhtml", adapter=EPUB_ADAPTER,
                                   source_locale=UNKNOWN_LOCALE, source_fingerprint=source_fingerprint)
    all_documents = (documents[0], second)
    all_machine = {unit.unit_id: html_unit_template(unit) + " [machine]"
                   for document in all_documents for unit in document.units}
    first = export_review_package(project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                                 format_version="3.0", documents=all_documents, machine_translations=all_machine)
    reverse = export_review_package(project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                                    format_version="3.0", documents=tuple(reversed(all_documents)),
                                    machine_translations=all_machine)

    canonical = encode_review_package(first)
    assert canonical == encode_review_package(reverse)
    assert canonical == encode_review_package(replace(first, rows=tuple(reversed(first.rows))))
    records = list(csv.reader(io.StringIO(canonical.decode("utf-8"), newline="")))
    decoded_reversed = decode_review_package(_csv_bytes(records[:2] + list(reversed(records[2:]))))
    assert encode_review_package(decoded_reversed) == canonical


def test_import_accepts_lf_records_but_export_recanonicalizes_to_crlf():
    *_context, package = make_context()
    canonical = encode_review_package(package)
    lf = canonical.replace(b"\r\n", b"\n")

    decoded = decode_review_package(lf)

    assert encode_review_package(decoded) == canonical


def test_empty_null_and_backslash_note_values_are_distinct():
    *_context, package = make_context()
    cases = (None, "", r"\N", "\\note")
    for value in cases:
        candidate = replace(package, rows=(replace(package.rows[0], reviewer_note=value),))
        decoded = decode_review_package(encode_review_package(candidate))
        assert decoded.rows[0].reviewer_note == value


@pytest.mark.parametrize("value", [
    "=1+1", " +1", "  @SUM(A1)", "\t=cmd()", " \t-1", "\r=1+1", "\n@cmd", "\ttext", "\rtext",
])
def test_formula_escape_is_reversible_after_spaces_tab_cr_and_lf(value):
    escaped = neutralize_formula_cell(value)
    assert escaped != value
    assert escaped.startswith("'⟪AYVU-FORMULA-ESCAPE-v1⟫")
    assert restore_formula_cell(escaped) == value
    edited = escaped + " edit"
    assert restore_formula_cell(edited) == value + " edit"


@pytest.mark.parametrize("value", ["it's--flag", "'ordinary", "prefix' = text", "safe @ home"])
def test_formula_escape_leaves_normal_apostrophes_and_prefixes_exact(value):
    assert neutralize_formula_cell(value) == value
    assert restore_formula_cell(value) == value


def test_a_user_supplied_exact_escape_prefix_is_doubled_then_restored():
    value = "'⟪AYVU-FORMULA-ESCAPE-v1⟫ordinary text"
    encoded = neutralize_formula_cell(value)
    assert encoded.startswith("'⟪AYVU-FORMULA-ESCAPE-v1⟫'⟪AYVU-FORMULA-ESCAPE-v1⟫")
    assert restore_formula_cell(encoded) == value


def test_package_formula_cells_survive_codec_and_human_edit():
    *_context, package = make_context()
    row = replace(package.rows[0], reviewed_text="\t=SUM(A1)", reviewer_note="=private note")
    decoded = decode_review_package(encode_review_package(replace(package, rows=(row,))))
    assert decoded.rows[0].reviewed_text == "\t=SUM(A1)"
    assert decoded.rows[0].reviewer_note == "=private note"


def test_path_codec_writer_is_explicit_bounded_and_does_not_echo_destination(tmp_path):
    *_context, package = make_context()
    output = tmp_path / "review.csv"

    written = write_review_package(output, package)
    assert written == output
    assert read_review_package(output) == package
    with pytest.raises(ReviewExchangeError) as caught:
        write_review_package(output, package)
    assert caught.value.code == "review_file_exists"
    assert str(output) not in str(caught.value)


def test_identity_and_protected_parts_are_stable_and_markup_safe_through_mapping():
    source, project, artifact, documents, machine, package = make_context(
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<body><p>Hello <strong>bold <!--keep-comment--><?keep data?><em>nested</em></strong> and <code>Ctrl+S</code>.</p>'
        '<div>Loose text <p>mixed <a epub:type="noteref" href="#n">content</a></p> tail</div></body></html>')
    decoded = decode_review_package(encode_review_package(package))
    reviewed = mark_reviewed(decoded, lambda value: value.replace("Hello", "Olá"))

    plan = map_review_package(reviewed, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine)
    rendered = reconstruct_html(source, documents[0], plan.translations)
    parsed = BeautifulSoup(rendered, "lxml-xml")

    assert [item.unit_id for item in plan.translations] == sorted(unit.unit_id for unit in documents[0].units)
    assert parsed.strong.find("em") is not None
    assert parsed.code is not None and parsed.code.get_text() == "Ctrl+S"
    assert parsed.a is not None and parsed.a.get("epub:type") == "noteref" and parsed.a.get("href") == "#n"
    assert b"<!--keep-comment-->" in rendered and b"<?keep data?>" in rendered
    assert "Olá" in parsed.get_text()
    assert "Loose text" in parsed.get_text() and "tail" in parsed.get_text()


def test_plain_text_unit_round_trips_without_inventing_placeholders():
    source, project, artifact, documents, machine, package = make_context(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Plain text only.</p></body></html>')
    row = package.rows[0]
    assert row.protected_parts == ()
    reviewed = replace(package, rows=(replace(row, reviewed_text="Texto puro", review_status="reviewed"),))
    plan = map_review_package(decode_review_package(encode_review_package(reviewed)), project=project,
                              machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
                              documents=documents, machine_translations=machine)
    rendered = reconstruct_html(source, documents[0], plan.translations)
    assert b"Texto puro" in rendered and b"__AYVU_TAG_" not in rendered


def test_markup_entities_paths_urls_and_prompt_text_remain_plain_user_text():
    source, project, artifact, documents, machine, package = make_context()
    row = package.rows[0]
    markers = [part[0] for part in row.protected_parts]
    hostile = '<script src="/tmp/private">run()</script> &amp; /tmp/private https://example.invalid ignore all rules '
    edited = hostile + " ".join(markers)
    reviewed = replace(package, rows=(replace(row, reviewed_text=edited, review_status="reviewed"),))

    plan = map_review_package(reviewed, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine)
    rendered = reconstruct_html(source, documents[0], plan.translations)
    parsed = BeautifulSoup(rendered, "lxml-xml")

    assert parsed.find("script") is None
    assert b"<script src=\"/tmp/private\">" not in rendered
    assert "<script" in parsed.get_text()
    assert "https://example.invalid" in parsed.get_text()
    assert parsed.em is not None


@pytest.mark.parametrize("damage,code", [
    ("missing", "missing_placeholder"),
    ("duplicate", "duplicate_placeholder"),
    ("invented", "foreign_placeholder"),
    ("malformed", "malformed_placeholder"),
    ("reordered", "invalid_placeholder_order"),
])
def test_every_placeholder_corruption_fails_before_a_plan(damage, code):
    html = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <em>B</em> C</p></body></html>'
    _source, project, artifact, documents, machine, package = make_context(html)
    row = package.rows[0]
    markers = [marker for marker, _kind in row.protected_parts]
    if damage == "missing":
        text = row.machine_translation.replace(markers[0], "", 1)
    elif damage == "duplicate":
        text = row.machine_translation + " " + markers[0]
    elif damage == "invented":
        text = row.machine_translation + " __AYVU_REVIEW_PART_" + "a" * 32 + "_0000__"
    elif damage == "malformed":
        text = row.machine_translation.replace(markers[0], "__AYVU_REVIEW_PART_broken__", 1)
    else:
        text = row.machine_translation.replace(markers[0], "TEMP", 1).replace(markers[1], markers[0], 1).replace("TEMP", markers[1], 1)
    reviewed = replace(package, rows=(replace(row, reviewed_text=text, review_status="reviewed"),))

    error_code(code, lambda: map_review_package(reviewed, project=project, machine_artifact=artifact,
                                                descriptor=DESCRIPTOR, format_version="3.0", documents=documents,
                                                machine_translations=machine))


def test_cross_unit_and_cross_package_placeholders_cannot_be_borrowed():
    html = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <em>one</em></p><p>B <strong>two</strong></p></body></html>'
    _source, project, artifact, documents, machine, package = make_context(html)
    first, second = package.rows
    first_marker = first.protected_parts[0][0]
    borrowed = second.protected_parts[0][0]
    reviewed = replace(package, rows=(
        replace(first, reviewed_text=first.machine_translation.replace(first_marker, borrowed), review_status="reviewed"),
        replace(second, review_status="reviewed"),
    ))
    error_code("foreign_placeholder", lambda: map_review_package(
        reviewed, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))

    other_artifact = replace(artifact, artifact_id=OTHER_ARTIFACT_ID)
    other_package = export_review_package(project=project, machine_artifact=other_artifact, descriptor=DESCRIPTOR,
                                          format_version="3.0", documents=documents,
                                          machine_translations=machine)
    original = package.rows[0]
    other_marker = other_package.rows[0].protected_parts[0][0]
    cross_package = replace(package, rows=(replace(original,
        reviewed_text=original.machine_translation.replace(original.protected_parts[0][0], other_marker),
        review_status="reviewed"), package.rows[1]))
    error_code("foreign_placeholder", lambda: map_review_package(
        cross_package, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))


@pytest.mark.parametrize("mutation,code", [
    ("duplicate", "duplicate_unit"),
    ("conflict", "conflicting_duplicate"),
    ("status", "invalid_review_status"),
])
def test_duplicate_conflicting_and_invalid_status_rows_are_structured_failures(mutation, code):
    *_context, package = make_context()
    row = package.rows[0]
    if mutation == "status":
        changed = replace(row, review_status="approved")
        candidate = replace(package, rows=(changed,))
    elif mutation == "conflict":
        candidate = replace(package, rows=(row, replace(row, reviewer_note="conflicting note")))
    else:
        candidate = replace(package, rows=(row, row))

    error_code(code, lambda: encode_review_package(candidate))


@pytest.mark.parametrize("mutation,code", [
    ("unknown_document", "unknown_document"),
    ("unknown_unit", "unknown_unit"),
    ("missing", "missing_unit"),
    ("pending", "review_not_approved"),
])
def test_mapping_rejects_unknown_missing_and_unreviewed_units(mutation, code):
    _source, project, artifact, documents, machine, package = make_context()
    reviewed = mark_reviewed(package)
    if mutation == "unknown_document":
        changed = replace(reviewed.rows[0], document_ref="doc:v1:" + "a" * 64)
        reviewed = replace(reviewed, rows=(changed,))
    elif mutation == "unknown_unit":
        changed = replace(reviewed.rows[0], unit_id="unit:v1:" + "a" * 64)
        reviewed = replace(reviewed, rows=(changed,))
    elif mutation == "missing":
        reviewed = replace(reviewed, rows=())
    else:
        reviewed = replace(package, rows=package.rows)

    error_code(code, lambda: map_review_package(reviewed, project=project, machine_artifact=artifact,
                                                descriptor=DESCRIPTOR, format_version="3.0", documents=documents,
                                                machine_translations=machine))


def test_source_machine_and_locator_fingerprints_are_exactly_bound():
    _source, project, artifact, documents, machine, package = make_context()
    row = package.rows[0]
    reviewed = mark_reviewed(package)
    bad_source = replace(reviewed, rows=(replace(reviewed.rows[0], source_text="changed"),))
    error_code("source_mismatch", lambda: map_review_package(
        bad_source, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))
    bad_machine = replace(reviewed, rows=(replace(reviewed.rows[0], machine_translation="different"),))
    error_code("machine_mismatch", lambda: map_review_package(
        bad_machine, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))
    bad_locator = replace(reviewed, rows=(replace(reviewed.rows[0], locator_fingerprint="sha256:" + "0" * 64),))
    error_code("incompatible_locator", lambda: map_review_package(
        bad_locator, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))
    assert row.source_digest == documents[0].units[0].source_digest


def test_stale_project_source_machine_artifact_and_adapter_are_rejected():
    _source, project, artifact, documents, machine, package = make_context()
    reviewed = mark_reviewed(package)
    error_code("stale_project_revision", lambda: map_review_package(
        reviewed, project=replace(project, revision=project.revision + 1), machine_artifact=artifact,
        descriptor=DESCRIPTOR, format_version="3.0", documents=documents, machine_translations=machine))
    wrong_project = ProjectManifest.create(
        ProjectRequest("/private/other.epub", project.source_size, project.source_sha256, "en", "pt"), OTHER_PROJECT_ID)
    error_code("ownership_mismatch", lambda: map_review_package(
        reviewed, project=wrong_project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))
    error_code("stale_machine_artifact", lambda: map_review_package(
        reviewed, project=project, machine_artifact=replace(artifact, revision=4), descriptor=DESCRIPTOR,
        format_version="3.0", documents=documents, machine_translations=machine))
    error_code("machine_artifact_mismatch", lambda: map_review_package(
        reviewed, project=project, machine_artifact=replace(artifact, artifact_id=OTHER_ARTIFACT_ID),
        descriptor=DESCRIPTOR, format_version="3.0", documents=documents, machine_translations=machine))
    error_code("machine_artifact_mismatch", lambda: map_review_package(
        reviewed, project=project, machine_artifact=replace(artifact, digest="sha256:" + "0" * 64),
        descriptor=DESCRIPTOR, format_version="3.0", documents=documents, machine_translations=machine))
    error_code("incompatible_format", lambda: map_review_package(
        reviewed, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="2.0",
        documents=documents, machine_translations=machine))
    edited_source = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Changed <em>reader</em></p></body></html>'
    edited_doc = extract_html_document(edited_source, PROJECT_ID, "OEBPS/text/chapter.xhtml", adapter=EPUB_ADAPTER,
                                       source_locale=UNKNOWN_LOCALE,
                                       source_fingerprint="sha256:" + hashlib.sha256(edited_source).hexdigest())
    error_code("source_mismatch", lambda: map_review_package(
        reviewed, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=(edited_doc,), machine_translations=machine))


def test_package_versions_columns_encoding_quoting_and_malformed_csv_fail_closed():
    *_context, package = make_context()
    encoded = encode_review_package(package)
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    records[0][1] = "9"
    error_code("unsupported_schema_version", lambda: decode_review_package(_csv_bytes(records)))
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    metadata = json.loads(records[0][2])
    metadata["schema_version"] = 9
    records[0][2] = canonical_json(metadata)
    error_code("unsupported_schema_version", lambda: decode_review_package(_csv_bytes(records)))
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    metadata = json.loads(records[0][2])
    metadata["codec_version"] = 9
    records[0][2] = canonical_json(metadata)
    error_code("unsupported_codec_version", lambda: decode_review_package(_csv_bytes(records)))
    records = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
    records[1].pop()
    error_code("incompatible_columns", lambda: decode_review_package(_csv_bytes(records)))
    error_code("invalid_encoding", lambda: decode_review_package(b"\xff"))
    error_code("invalid_encoding", lambda: decode_review_package("\ufeff".encode("utf-16") + encoded))
    error_code("malformed_csv", lambda: decode_review_package(b'"unterminated\r\n'))
    error_code("invalid_newline", lambda: decode_review_package(encoded.replace(b"\r\n", b"\r", 1)))


def _csv_bytes(records):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(records)
    return stream.getvalue().encode("utf-8")


@pytest.mark.parametrize("limit_name,value,code", [
    ("MAX_REVIEW_RAW_BYTES", 4, "file_too_large"),
    ("MAX_REVIEW_ROWS", 0, "too_many_rows"),
    ("MAX_REVIEW_COLUMNS", len(REVIEW_CSV_COLUMNS) - 1, "invalid_column_count"),
    ("MAX_REVIEW_CELL_CHARS", 1, "cell_too_large"),
    ("MAX_REVIEW_TOTAL_CHARS", 1, "decoded_content_too_large"),
    ("MAX_REVIEW_METADATA_CHARS", 1, "metadata_too_large"),
    ("MAX_REVIEW_METADATA_ITEMS", 1, "metadata_too_many_items"),
    ("MAX_REVIEW_METADATA_DEPTH", 2, "metadata_too_deep"),
    ("MAX_REVIEW_DOCUMENTS", 0, "too_many_documents"),
    ("MAX_REVIEW_NOTE_CHARS", 1, "invalid_reviewer_note"),
])
def test_each_exchange_limit_has_a_bounded_failure(monkeypatch, limit_name, value, code):
    import ayvu.review_exchange as exchange

    *_context, package = make_context()
    if limit_name == "MAX_REVIEW_NOTE_CHARS":
        package = replace(package, rows=(replace(package.rows[0], reviewer_note="xx"),))
    encoded = encode_review_package(package)
    monkeypatch.setattr(exchange, limit_name, value)
    error_code(code, lambda: decode_review_package(encoded))


def test_protected_part_cardinality_is_bounded(monkeypatch):
    import ayvu.review_exchange as exchange

    html = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <em>B</em> C</p></body></html>'
    _source, project, artifact, documents, machine, _package = make_context(html)
    monkeypatch.setattr(exchange, "MAX_REVIEW_PARTS_PER_UNIT", 1)
    error_code("too_many_protected_parts", lambda: export_review_package(
        project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))


def test_path_reader_requests_only_one_byte_over_its_raw_limit(monkeypatch):
    import ayvu.review_exchange as exchange

    calls = []

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, count):
            calls.append(count)
            return b"x" * count

    class FakePath:
        def __init__(self, _path):
            pass

        def open(self, _mode):
            return Reader()

    monkeypatch.setattr(exchange, "MAX_REVIEW_RAW_BYTES", 7)
    monkeypatch.setattr(exchange, "Path", FakePath)
    error_code("file_too_large", lambda: read_review_package("/private/secret.csv"))
    assert calls == [8]


def test_v2_column_cardinality_fails_before_csv_materializes_cells(monkeypatch):
    import ayvu.review_exchange as exchange

    monkeypatch.setattr(exchange.csv, "reader", lambda *_args, **_kwargs: pytest.fail("csv.reader must not run"))
    raw = b"x," * (MAX_REVIEW_COLUMNS * 1000)
    error_code("invalid_column_count", lambda: decode_review_package(raw))


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


@pytest.mark.parametrize("resave", [
    lambda raw: _saved_by_a_spreadsheet(raw),
    lambda raw: _saved_by_a_spreadsheet(raw, quoting=csv.QUOTE_ALL, terminator="\r\n"),
    lambda raw: _saved_by_a_spreadsheet(raw, delimiter=";"),
    lambda raw: _saved_by_a_spreadsheet(raw, delimiter=";", quoting=csv.QUOTE_ALL),
    lambda raw: b"\xef\xbb\xbf" + _saved_by_a_spreadsheet(raw, delimiter=";"),
    lambda raw: b"\xef\xbb\xbf" + raw,
    lambda raw: raw + b"\r\n",
], ids=["padded", "quote-all", "semicolon", "semicolon-quote-all", "bom-semicolon", "bom", "blank-final-record"])
def test_a_package_saved_by_a_spreadsheet_still_decodes_to_the_same_canonical_bytes(resave):
    _source, project, artifact, documents, machine, package = make_context()
    row = replace(package.rows[0], reviewed_text=f'{package.rows[0].reviewed_text}; "sim", =não', reviewer_note="a;b,c")
    package = mark_reviewed(replace(package, rows=(row,)))
    encoded = encode_review_package(package)
    decoded = decode_review_package(resave(encoded))
    assert encode_review_package(decoded) == encoded
    assert map_review_package(decoded, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine).translations


@pytest.mark.parametrize("damage,code", [
    (lambda raw: raw.replace(b"\r\n", b",value\r\n", 1), "invalid_preamble"),
    (lambda raw: raw.replace(b"AYVU-REVIEW-PACKAGE,", b"AYVU-REVIEW-PACKAGE;", 1), "invalid_preamble"),
    (lambda raw: raw + b"not,a,row\r\n", "invalid_column_count"),
], ids=["value-after-the-preamble", "mixed-delimiters", "short-row"])
def test_spreadsheet_tolerance_only_covers_empty_cells_and_one_delimiter(damage, code):
    *_rest, package = make_context()
    error_code(code, lambda: decode_review_package(damage(encode_review_package(mark_reviewed(package)))))


def test_read_review_package_does_not_echo_an_unavailable_path(tmp_path):
    private_path = tmp_path / "PRIVATE_PATH_CANARY_148.csv"
    with pytest.raises(ReviewExchangeError) as caught:
        read_review_package(private_path)
    assert caught.value.code == "review_file_not_found"
    assert "PRIVATE_PATH_CANARY_148" not in str(caught.value)


def test_legacy_v1_is_boundedly_inspectable_redacted_and_never_applicable(tmp_path, monkeypatch):
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
    output = tmp_path / "must-not-exist.epub"
    for _plain_or_inline in ("plain", "inline"):
        with pytest.raises(ReviewImportError) as caught:
            from ayvu.epub_io import apply_reviewed_epub

            apply_reviewed_epub(tmp_path / "not-read.epub", output, inspection)
        assert caught.value.code == "legacy_reexport_required"
        assert not output.exists()

    class ExtendedLegacyInspection(LegacyReviewInspection):
        pass

    with pytest.raises(ReviewImportError) as subclass_result:
        from ayvu.epub_io import apply_reviewed_epub

        apply_reviewed_epub(tmp_path / "not-read.epub", output,
                            ExtendedLegacyInspection(inspection.rows, inspection.duplicate_ids, inspection.warnings))
    assert subclass_result.value.code == "legacy_reexport_required"

    monkeypatch.setattr("ayvu.review_import.MAX_REVIEW_BYTES", 3)
    with pytest.raises(ReviewImportError) as oversized:
        read_review_csv(csv_path)
    assert oversized.value.code == "review_file_too_large"
    assert path_canary not in str(oversized.value)


def test_v1_positional_text_matches_never_become_v2_identity():
    legacy = ReviewImportData((ReviewRow("c0001-s0001", "chapter.xhtml", "same text", "same text"),), ())
    error_code("invalid_package", lambda: map_review_package(
        legacy, project=None, machine_artifact=None, descriptor=None, format_version="3.0",
        documents=(), machine_translations={}))


def test_sensitive_content_paths_and_notes_stay_out_of_repr_errors_and_logs(caplog):
    canaries = ("source-canary-148", "machine-canary-148", "review-canary-148", "note-canary-148", "/private/path-canary-148")
    _source, project, artifact, documents, machine, package = make_context()
    row = replace(package.rows[0], source_text=canaries[0], machine_translation=canaries[1],
                  reviewed_text=canaries[2], reviewer_note=canaries[3] + canaries[4])
    candidate = replace(package, rows=(row,))
    renderings = (repr(candidate), repr(row), repr(candidate.header))
    assert all(canary not in rendered for canary in canaries for rendered in renderings)
    caplog.set_level("DEBUG")
    with pytest.raises(ReviewExchangeError) as caught:
        map_review_package(candidate, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                           format_version="3.0", documents=documents, machine_translations=machine)
    assert caught.value.code == "source_mismatch"
    assert not any(canary in repr(caught.value.failure) for canary in canaries)
    assert not any(canary in str(caught.value) for canary in canaries)
    assert not any(canary in caplog.text for canary in canaries)


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


def test_production_epub_extraction_review_mapping_and_staged_html_reconstruction(monkeypatch):
    xhtml = (
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
        b'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head><body>'
        b'<p>Hello <strong>bold <em>nested</em></strong> and <code>Ctrl+S</code>.</p>'
        b'<div>Loose <p>mixed <a epub:type="noteref" href="#n">content</a></p> tail</div>'
        b'</body></html>')
    source_bytes = epub_bytes(replace={"OEBPS/text/one.xhtml": xhtml})
    source = MemorySource(source_bytes)
    project_sha = hashlib.sha256(source_bytes).hexdigest()
    project = ProjectManifest.create(ProjectRequest("/secret/book.epub", len(source_bytes), project_sha, "en", "pt"), PROJECT_ID)
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    service = FormatService(registry)
    result = service.extract("epub", source, ExtractionRequest(PROJECT_ID, frozenset({ContentScope.BODY})))
    assert result.failure is None
    documents = result.documents
    artifact = Artifact(ARTIFACT_ID, ArtifactRequest(
        PROJECT_ID, JOB_ID, "raw_results", "translate", 1, "sha256:" + "e" * 64, "application/json"),
        ArtifactState.READY, 3, NOW, NOW, 321, "sha256:" + "f" * 64)
    machine = {unit.unit_id: html_unit_template(unit) + " machine" for doc in documents for unit in doc.units}
    package = export_review_package(project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                                    format_version="3.0", documents=documents, machine_translations=machine)
    imported = decode_review_package(encode_review_package(package))
    edited = mark_reviewed(imported, lambda text: text.replace("Hello", "Olá"))

    # Instrument real effect ports and the reconstructor. The mapper must stop
    # before any of them; only the explicit staged step below reconstructs HTML.
    transport_calls = []
    socket_calls = []
    publisher_calls = []
    provenance_calls = []
    event_calls = []
    reconstruction_calls = []
    real_reconstruct = reconstruct_html

    def instrumented_reconstruct(*args, **kwargs):
        reconstruction_calls.append(1)
        return real_reconstruct(*args, **kwargs)

    monkeypatch.setattr("requests.Session.request", lambda *args, **kwargs: transport_calls.append((args, kwargs)))
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: socket_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.artifact_service.ArtifactService.publish",
                        lambda *args, **kwargs: publisher_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.provenance_store.SqliteProvenanceRepository.append",
                        lambda *args, **kwargs: provenance_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.job_execution.InMemoryJobEventLog.emit",
                        lambda *args, **kwargs: event_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.html_structure.reconstruct_html", instrumented_reconstruct)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected file IO")))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected persistence")))
    plan = map_review_package(edited, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine)
    assert reconstruction_calls == []
    assert publisher_calls == [] and transport_calls == [] and socket_calls == []
    assert provenance_calls == [] and event_calls == []
    with ZipFile(io.BytesIO(source_bytes)) as archive:
        original_html = archive.read(documents[0].logical_key)
    staged_html = instrumented_reconstruct(original_html, documents[0], tuple(
        item for item in plan.translations if item.unit_id in {unit.unit_id for unit in documents[0].units}))

    assert reconstruction_calls == [1]
    assert publisher_calls == [] and transport_calls == [] and socket_calls == []
    assert provenance_calls == [] and event_calls == []
    parsed = BeautifulSoup(staged_html, "lxml-xml")
    assert parsed.find("script") is None
    assert parsed.code.get_text() == "Ctrl+S"
    assert parsed.strong.find("em") is not None
    assert parsed.a.get("epub:type") == "noteref" and parsed.a.get("href") == "#n"
    assert "Olá" in parsed.get_text() and "tail" in parsed.get_text()

