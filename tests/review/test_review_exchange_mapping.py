from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from ayvu.content.model import UNKNOWN_LOCALE
from ayvu.formats.epub.adapter import EPUB_ADAPTER
from ayvu.formats.epub.html_structure import extract_html_document, reconstruct_html
from ayvu.projects.model import ProjectManifest, ProjectRequest
from ayvu.review.exchange_codec import decode_review_package, encode_review_package
from ayvu.review.exchange import export_review_package
from ayvu.review.exchange_mapping import map_review_package

from tests.review.review_exchange_support import DESCRIPTOR
from tests.review.review_exchange_support import OTHER_ARTIFACT_ID, OTHER_PROJECT_ID, PROJECT_ID
from tests.review.review_exchange_support import error_code, make_context, mark_reviewed


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
    ("rejected", "review_rejected"),
    ("edited_pending", "pending_row_edited"),
])
def test_mapping_rejects_unknown_missing_and_unusable_rows(mutation, code):
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
    elif mutation == "rejected":
        reviewed = replace(reviewed, rows=(replace(reviewed.rows[0], review_status="rejected"), *reviewed.rows[1:]))
    elif mutation == "edited_pending":
        edited = replace(reviewed.rows[0], review_status="pending", reviewed_text=reviewed.rows[0].reviewed_text + " x")
        reviewed = replace(reviewed, rows=(edited, *reviewed.rows[1:]))
    else:
        reviewed = replace(package, rows=package.rows)

    error_code(code, lambda: map_review_package(reviewed, project=project, machine_artifact=artifact,
                                                descriptor=DESCRIPTOR, format_version="3.0", documents=documents,
                                                machine_translations=machine))


def test_a_pending_row_keeps_the_machine_translation_while_a_reviewed_one_changes():
    _source, project, artifact, documents, machine, package = make_context(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>First line.</p><p>Second line.</p></body></html>')
    assert len(package.rows) == 2
    marked = replace(package.rows[0], reviewed_text=package.rows[0].machine_translation + " HUMAN", review_status="reviewed")
    partial = replace(package, rows=(marked, *package.rows[1:]))

    plan = map_review_package(partial, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine)

    by_unit = {item.unit_id: item.text for item in plan.translations}
    assert len(by_unit) == len(package.rows)
    assert "HUMAN" in by_unit[marked.unit_id]
    for row in package.rows[1:]:
        assert by_unit[row.unit_id] == machine[row.unit_id]


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
