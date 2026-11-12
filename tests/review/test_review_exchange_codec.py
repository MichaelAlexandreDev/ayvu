from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import replace

import pytest

from ayvu.content.model import UNKNOWN_LOCALE
from ayvu.formats.epub.adapter import EPUB_ADAPTER
from ayvu.formats.epub.html_structure import extract_html_document, html_unit_template
from ayvu.review import exchange_codec, exchange_files, exchange_model
from ayvu.review.exchange_model import MAX_REVIEW_COLUMNS, REVIEW_CSV_COLUMNS, REVIEW_CSV_PREAMBLE, ReviewExchangeError, canonical_json
from ayvu.review.exchange_codec import decode_review_package, encode_review_package, neutralize_formula_cell, restore_formula_cell
from ayvu.review.exchange import export_review_package
from ayvu.review.exchange_mapping import map_review_package
from ayvu.review.exchange_files import read_review_package, write_review_package

from tests.review.review_exchange_support import DESCRIPTOR
from tests.review.review_exchange_support import PROJECT_ID, _csv_bytes, _saved_by_a_spreadsheet
from tests.review.review_exchange_support import error_code, make_context, mark_reviewed


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


@pytest.mark.parametrize("limit_name,value,code,modules", [
    ("MAX_REVIEW_RAW_BYTES", 4, "file_too_large", (exchange_codec,)),
    ("MAX_REVIEW_ROWS", 0, "too_many_rows", (exchange_codec, exchange_model)),
    ("MAX_REVIEW_COLUMNS", len(REVIEW_CSV_COLUMNS) - 1, "invalid_column_count", (exchange_codec,)),
    ("MAX_REVIEW_CELL_CHARS", 1, "cell_too_large", (exchange_codec, exchange_model)),
    ("MAX_REVIEW_TOTAL_CHARS", 1, "decoded_content_too_large", (exchange_codec,)),
    ("MAX_REVIEW_METADATA_CHARS", 1, "metadata_too_large", (exchange_codec,)),
    ("MAX_REVIEW_METADATA_ITEMS", 1, "metadata_too_many_items", (exchange_codec,)),
    ("MAX_REVIEW_METADATA_DEPTH", 2, "metadata_too_deep", (exchange_codec,)),
    ("MAX_REVIEW_DOCUMENTS", 0, "too_many_documents", (exchange_model,)),
    ("MAX_REVIEW_NOTE_CHARS", 1, "invalid_reviewer_note", (exchange_model,)),
])
def test_each_exchange_limit_has_a_bounded_failure(monkeypatch, limit_name, value, code, modules):
    *_context, package = make_context()
    if limit_name == "MAX_REVIEW_NOTE_CHARS":
        package = replace(package, rows=(replace(package.rows[0], reviewer_note="xx"),))
    encoded = encode_review_package(package)
    for module in modules:
        monkeypatch.setattr(module, limit_name, value)
    error_code(code, lambda: decode_review_package(encoded))


def test_protected_part_cardinality_is_bounded(monkeypatch):
    import ayvu.review.exchange as exchange

    html = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <em>B</em> C</p></body></html>'
    _source, project, artifact, documents, machine, _package = make_context(html)
    monkeypatch.setattr(exchange, "MAX_REVIEW_PARTS_PER_UNIT", 1)
    monkeypatch.setattr(exchange_model, "MAX_REVIEW_PARTS_PER_UNIT", 1)
    error_code("too_many_protected_parts", lambda: export_review_package(
        project=project, machine_artifact=artifact, descriptor=DESCRIPTOR, format_version="3.0",
        documents=documents, machine_translations=machine))


def test_path_reader_requests_only_one_byte_over_its_raw_limit(monkeypatch):
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

    monkeypatch.setattr(exchange_files, "MAX_REVIEW_RAW_BYTES", 7)
    monkeypatch.setattr(exchange_codec, "MAX_REVIEW_RAW_BYTES", 7)
    monkeypatch.setattr(exchange_files, "Path", FakePath)
    error_code("file_too_large", lambda: read_review_package("/private/secret.csv"))
    assert calls == [8]


def test_v2_column_cardinality_fails_before_csv_materializes_cells(monkeypatch):
    monkeypatch.setattr(exchange_codec.csv, "reader", lambda *_args, **_kwargs: pytest.fail("csv.reader must not run"))
    raw = b"x," * (MAX_REVIEW_COLUMNS * 1000)
    error_code("invalid_column_count", lambda: decode_review_package(raw))


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
