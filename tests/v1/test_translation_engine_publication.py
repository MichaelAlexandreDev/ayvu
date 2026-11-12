from __future__ import annotations

from zipfile import ZIP_DEFLATED, ZIP_STORED

import pytest

from ayvu.v1.cache import TranslationCache
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.contracts import MaterializationResult
from ayvu.v1.translation import translate_book
from tests.support.epub_fakes import OPF, epub_bytes, members, zip_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv
from ayvu.kernel import ChapterSelection, ChapterSelectionError, TranslationOptions, JobFailure, JobFailureFamily

from tests.v1.translation_engine_support import HARD_CHAPTER, OPF_MEMBER, PAIR, Prefix
from tests.v1.translation_engine_support import book, engine_only, failing_on, formats


def test_fail_fast_with_workers_stops_and_publishes_nothing(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ConnectionError):
        translate_book(source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2, fail_fast=True),
                       formats=formats, source=LocalFormatSource(source), publication=publication, project_id=PROJECT,
                       translator_factory=lambda: Prefix(failing_on("Second")), cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert not env.is_ready(publication)


def test_a_book_the_adapter_cannot_rebuild_reports_no_output(tmp_path, formats, monkeypatch):
    source = book(tmp_path)
    failure = JobFailure(JobFailureFamily.INTEGRITY, "source_changed")
    monkeypatch.setattr(formats, "reconstruct", lambda *_args, **_kwargs: MaterializationResult(failure=failure))
    env = FormatEnv(tmp_path / "env")
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR), formats=formats,
                                        source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT)
    assert result.failure == failure and result.artifact is None and not result.output_written and not report.output_written


@pytest.mark.parametrize("build,code", [
    (lambda: epub_bytes(add=[("../evil", b"x", ZIP_STORED)]), "unsafe_member_name"),
    (lambda: epub_bytes(add=[("OEBPS\\x.txt", b"x", ZIP_DEFLATED)]), "unsafe_member_name"),
    (lambda: zip_bytes([*members(), members()[3]]), "duplicate_member"),
    (lambda: zip_bytes([*members(), (members()[3][0].upper(), b"x", ZIP_DEFLATED)]), "duplicate_member"),
    (lambda: epub_bytes(replace={"OEBPS/text/one.xhtml": b'<!DOCTYPE html [<!ENTITY e "E">]><html><body><p>A &e; B</p></body></html>'}), "entity_declaration"),
    (lambda: epub_bytes(add=[("META-INF/encryption.xml", b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/><enc:CipherData><enc:CipherReference URI="OEBPS/text/one.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>', ZIP_DEFLATED)]), "encrypted_content"),
], ids=["traversal", "backslash", "duplicate", "case-duplicate", "entity", "encrypted"])
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_a_refused_book_calls_no_provider_and_publishes_nothing(tmp_path, formats, build, code):
    report, result, translator, written = engine_only(tmp_path, formats, build())
    assert result.failure.code == code and not translator.calls and written is None and not report.output_written


@pytest.mark.parametrize("data", [
    epub_bytes(drop=("mimetype",)),
    zip_bytes([*members()[1:], members()[0]]),
    zip_bytes([("mimetype", b"application/epub+zip", ZIP_DEFLATED), *members()[1:]]),
    epub_bytes(replace={"mimetype": b"text/plain"}),
], ids=["missing", "not-first", "compressed", "other-content"])
def test_a_defective_mimetype_is_translated_and_comes_out_correct(tmp_path, formats, data):
    _, result, translator, written = engine_only(tmp_path, formats, data)
    assert result.failure is None and translator.calls
    assert next(iter(written)) == "mimetype" and written["mimetype"] == b"application/epub+zip"
    assert b"PT:Hello" in written["OEBPS/text/one.xhtml"]


def test_a_remote_manifest_item_no_longer_stops_a_book(tmp_path, formats):
    _, result, translator, written = engine_only(tmp_path, formats, epub_bytes())
    assert result.failure is None and translator.calls and b"PT:Hello" in written["OEBPS/text/one.xhtml"]


def test_a_processing_instruction_in_the_metadata_no_longer_stops_a_book(tmp_path, formats):
    opf = OPF.replace(b"<metadata>", b"<metadata><?keep this?>", 1)
    _, result, _, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True)
    assert result.failure is None and b"<?keep this?>" in written[OPF_MEMBER] and b"PT:The Title" in written[OPF_MEMBER]


def test_a_document_with_nothing_to_translate_keeps_its_bytes(tmp_path, formats):
    empty = b"<html><body><img src='../images/a.png'/></body></html>"
    _, result, _, written = engine_only(tmp_path, formats, epub_bytes(replace={"OEBPS/text/one.xhtml": empty}))
    assert result.failure is None and written["OEBPS/text/one.xhtml"] == empty


@pytest.mark.parametrize("title", [b"The<!-- c --> Title", b"<!-- c -->The Title", b"The <span>Title</span>", b"The<?keep this?> Title"])
def test_a_title_holding_other_nodes_is_reported_and_keeps_the_package_bytes(tmp_path, formats, title):
    opf = OPF.replace(b"The Title", title, 1)
    report, result, translator, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True)
    assert written[OPF_MEMBER] == opf and result.failure is None
    assert report.errors == ["metadata title: the title holds a comment, element or entity, so it stays untranslated"]
    assert not [call for call in translator.calls if "Title" in call]


def test_a_title_holding_other_nodes_stops_a_fail_fast_run(tmp_path, formats):
    opf = OPF.replace(b"The Title", b"The<!-- c --> Title", 1)
    with pytest.raises(ValueError, match="stays untranslated"):
        engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True, fail_fast=True)


def test_a_title_holding_other_nodes_is_ignored_without_translate_metadata(tmp_path, formats):
    opf = OPF.replace(b"The Title", b"The<!-- c --> Title", 1)
    report, _, _, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}))
    assert report.errors == [] and written[OPF_MEMBER] == opf


def test_a_title_translation_xml_cannot_hold_leaves_the_package_bytes_and_is_reported(tmp_path, formats):
    report, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(), translate_metadata=True, change=lambda text: f"a\x0bb {text}" if "The Title" in text else f"PT:{text}")
    assert written[OPF_MEMBER] == OPF and result.failure is None
    assert [error for error in report.errors if error.startswith("metadata title: ")]


def test_invalid_characters_in_a_translation_are_dropped_without_losing_text(tmp_path, formats):
    """Differs from the current flow on purpose: there, the same text also loses its ``&`` and ``<tags>``."""
    _, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(replace={"OEBPS/text/one.xhtml": HARD_CHAPTER}), change=lambda text: f"a\x0bb\x00c {text}")
    chapter = written["OEBPS/text/one.xhtml"]
    assert result.failure is None and b"\x0b" not in chapter and b"\x00" not in chapter
    assert b"abc Text &amp; more &lt;tags&gt;" in chapter


def test_an_alt_text_with_invalid_characters_still_gives_a_valid_document(tmp_path, formats):
    _, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(), translate_alt_text=True, change=lambda text: f"a\x0bb {text}")
    assert result.failure is None and b"\x0b" not in written["OEBPS/text/one.xhtml"]


def test_a_selection_that_matches_nothing_raises_before_any_call(tmp_path, formats):
    source = book(tmp_path)
    translator = Prefix()
    env = FormatEnv(tmp_path / "env")
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ChapterSelectionError, match="no chapters matched selection: 99"):
        translate_book(source, tmp_path / "out.epub", translator, cache,
                       TranslationOptions(language_pair=PAIR, chapter_selection=ChapterSelection.parse("99")), formats=formats,
                       source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT)
    assert not translator.calls


def test_fail_fast_stops_at_the_first_failed_text_and_publishes_nothing(tmp_path, formats):
    source = book(tmp_path)
    translator = Prefix(failing_on("Text"))
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ConnectionError):
        translate_book(source, tmp_path / "out.epub", translator, cache, TranslationOptions(language_pair=PAIR, fail_fast=True),
                       formats=formats, source=LocalFormatSource(source), publication=publication, project_id=PROJECT)
    assert not env.is_ready(publication)
