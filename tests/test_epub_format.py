import io
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from ayvu import epub_format, epub_io
from ayvu.artifact import ArtifactStoreError
from ayvu.cache import TranslationCache
from ayvu.domain import LanguagePair, TranslationOptions
from ayvu.epub_archive import EpubArchiveError
from ayvu.epub_format import EPUB_REGISTRATION, epub_format_adapter
from ayvu.format_adapter import (
    ContentScope, DetectionOutcome, ExtractionRequest, FidelityAspect, FidelityLevel, FindingSeverity, FormatOperation, OperationSupport, PreviewRequest,
    ReconstructionRequest, UnitTranslation,
)
from ayvu.format_local import LocalFormatSource, LocalOutputGuard
from ayvu.format_service import FormatRegistry, FormatService, format_safe_points
from ayvu.html_structure import html_unit_template
from ayvu.job_events import JobFailureFamily
from epub_fakes import OPF, TWO, XHTML, epub_bytes, members, opf_with, zip_bytes
from format_contract import PROJECT, FormatAdapterContract, FormatEnv, FormatHarness, TriggerToken


def translation_of(unit, index):
    return f"{html_unit_template(unit)} translated {index}"


def readable(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return b"\n".join(archive.read(info) for info in archive.infolist())


class TestEpubContract(FormatAdapterContract):
    @pytest.fixture
    def harness(self):
        return FormatHarness(epub_format_adapter(), EPUB_REGISTRATION, epub_bytes(), epub_bytes(replace={"OEBPS/text/one.xhtml": TWO}), ".epub", translation_of, readable)


ALL_SCOPES = frozenset(ContentScope)


@pytest.fixture
def service():
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def source_of(tmp_path, data=None, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(epub_bytes() if data is None else data)
    return path, LocalFormatSource(path)


def extract(service, source, scopes=ALL_SCOPES, **options):
    return service.extract("epub", source, ExtractionRequest(PROJECT, scopes, **options))


def all_units(documents):
    return [unit for document in documents for unit in document.units]


def done(documents, change=lambda text: text):
    return tuple(UnitTranslation(unit.unit_id, change(html_unit_template(unit))) for unit in all_units(documents))


def members_of(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def rebuild(service, env, source, documents, translations=None, *, kind=ReconstructionRequest, guard=None):
    publication = env.publication(guard)
    request = kind(documents, done(documents) if translations is None else translations)
    method = service.reconstruct if kind is ReconstructionRequest else service.preview
    result = method("epub", source, request, publication)
    return result, publication


# What it claims

def test_the_descriptor_states_every_operation_as_supported_with_evidence():
    descriptor = epub_format_adapter().descriptor
    for operation in FormatOperation:
        claim = descriptor.claim(operation)
        assert claim.support is OperationSupport.SUPPORTED and claim.evidence and claim.safe_points


def test_the_descriptor_states_scopes_and_fidelity_separately():
    descriptor = epub_format_adapter().descriptor
    assert descriptor.content_scopes == ALL_SCOPES
    assert {descriptor.fidelity_of(aspect) for aspect in FidelityAspect} == {FidelityLevel.PRESERVED}
    assert ".epub" in descriptor.extensions and "application/epub+zip" in descriptor.media_types


# Detection

def test_a_valid_epub_is_detected_and_a_renamed_one_too(service, tmp_path):
    _, source = source_of(tmp_path)
    assert service.detect(source).adapter_id == "epub"
    _, renamed = source_of(tmp_path, name="book.zip")
    assert service.detect(renamed).adapter_id == "epub"


@pytest.mark.parametrize("data", [
    b"not a zip at all", zip_bytes([("a.txt", b"x", ZIP_STORED)]), zip_bytes([*members()[1:], members()[0]]),
    zip_bytes([("mimetype", b"application/epub+zip", ZIP_DEFLATED), *members()[1:]]), epub_bytes(replace={"mimetype": b"text/plain"}),
], ids=range(5))
def test_the_extension_alone_is_not_believed(service, tmp_path, data):
    _, source = source_of(tmp_path, data)
    result = service.detect(source)
    assert (result.outcome, result.reason) == (DetectionOutcome.NO_MATCH, "signature_conflict")


@pytest.mark.parametrize("data,family,code", [
    (b"PK\x03\x04broken", JobFailureFamily.INTEGRITY, "corrupt_archive"),
    (epub_bytes(drop=("OEBPS/content.opf",)), JobFailureFamily.INPUT, "missing_package"),
    (epub_bytes(replace={"mimetype": b"text/plain"}), JobFailureFamily.INPUT, "invalid_mimetype"),
    (epub_bytes(add=[("../evil", b"x", ZIP_STORED)]), JobFailureFamily.INPUT, "unsafe_member_name"),
    (epub_bytes(add=[("OEBPS/bomb", b"\0" * (3 * 1024 * 1024), ZIP_DEFLATED)]), JobFailureFamily.RESOURCE, "suspicious_compression"),
    (epub_bytes(replace={"OEBPS/content.opf": b'<!DOCTYPE x [<!ENTITY e "v">]>' + OPF.split(b"?>", 1)[1]}), JobFailureFamily.POLICY, "entity_declaration"),
], ids=lambda value: value if isinstance(value, str) else None)
@pytest.mark.parametrize("operation", ["inspect", "extract", "validate"])
def test_what_detection_cannot_see_comes_back_from_each_operation(service, tmp_path, data, family, code, operation):
    path, source = source_of(tmp_path, data)
    before = path.read_bytes()
    result = extract(service, source) if operation == "extract" else getattr(service, operation)("epub", source)
    assert (result.failure.family, result.failure.code) == (family, code)
    assert path.read_bytes() == before


def test_protected_content_is_a_policy_failure(service, tmp_path):
    encrypted = (b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:e="http://www.w3.org/2001/04/xmlenc#"><e:EncryptedData>'
                 b'<e:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/><e:CipherData><e:CipherReference URI="OEBPS/text/one.xhtml"/></e:CipherData></e:EncryptedData></encryption>')
    _, source = source_of(tmp_path, epub_bytes(add=[("META-INF/encryption.xml", encrypted, ZIP_DEFLATED)]))
    failure = extract(service, source).failure
    assert (failure.family, failure.code) == (JobFailureFamily.POLICY, "encrypted_content")


def test_a_source_that_cannot_be_opened_is_a_code(service, tmp_path):
    class Vanishing:
        extension = ".epub"

        def __init__(self, inner):
            self.inner = inner

        def size(self):
            return self.inner.size()

        def fingerprint(self):
            return self.inner.fingerprint()

        def head(self, limit):
            return self.inner.head(limit)

        def open(self):
            raise OSError("/private/hunter2")

    _, source = source_of(tmp_path)
    result = service.inspect("epub", Vanishing(source))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.RESOURCE, "source_unreadable")
    assert "hunter2" not in repr(result)


# Inspection

def test_inspection_reports_the_book_and_what_a_selection_can_name(service, tmp_path):
    _, source = source_of(tmp_path)
    inventory = service.inspect("epub", source).inventory
    assert (inventory.title, inventory.authors, inventory.language, inventory.format_version) == ("The Title", ("An Author", "Another"), "en", "3.0")
    assert inventory.resource_count == 6
    assert [(item.logical_key, item.title) for item in inventory.documents] == [
        ("OEBPS/content.opf", "The Title"), ("OEBPS/nav.xhtml", "Doc"), ("OEBPS/text/one.xhtml", "Doc"), ("OEBPS/text/two words.xhtml", "Doc")]
    assert inventory.document_count == 4


def test_an_unknown_package_version_is_reported_as_unknown(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'version="3.0"', b'version="9.9"')))
    assert service.inspect("epub", source).inventory.format_version is None


def test_a_book_with_no_title_lists_only_its_documents(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'<dc:title id="t">The Title</dc:title>', b"")))
    inventory = service.inspect("epub", source).inventory
    assert inventory.title is None and inventory.document_count == 3


# Extraction

def test_body_only_is_the_default_scope(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    assert [document.logical_key for document in documents] == ["OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]
    assert "A picture" not in [unit.source_text for unit in all_units(documents)]


@pytest.mark.parametrize("scopes,keys", [
    ({ContentScope.BODY}, ["OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]),
    ({ContentScope.NAVIGATION}, ["OEBPS/nav.xhtml"]),
    ({ContentScope.METADATA}, ["OEBPS/content.opf"]),
    ({ContentScope.METADATA, ContentScope.BODY}, ["OEBPS/content.opf", "OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]),
], ids=range(4))
def test_each_scope_is_separate(service, tmp_path, scopes, keys):
    _, source = source_of(tmp_path)
    assert [document.logical_key for document in extract(service, source, frozenset(scopes)).documents] == keys


def test_alt_text_comes_only_when_asked_for(service, tmp_path):
    _, source = source_of(tmp_path)
    with_alt = all_units(extract(service, source, frozenset({ContentScope.BODY, ContentScope.IMAGE_ALT_TEXT})).documents)
    assert "A picture" in [unit.source_text for unit in with_alt]


def test_alt_text_alone_is_refused_rather_than_ignored(service, tmp_path):
    path, source = source_of(tmp_path)
    failure = extract(service, source, frozenset({ContentScope.IMAGE_ALT_TEXT})).failure
    assert (failure.family, failure.code) == (JobFailureFamily.CAPABILITY, "unsupported_scope")


def test_the_title_is_a_unit_with_its_own_locator(service, tmp_path):
    _, source = source_of(tmp_path)
    document = extract(service, source, frozenset({ContentScope.METADATA})).documents[0]
    unit = document.units[0]
    assert (unit.source_text, unit.content.locator.schema, unit.content.kind.name) == ("The Title", "epub_package", "title")


def test_a_book_with_a_blank_title_has_no_title_unit(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b">The Title<", b">   <")))
    assert extract(service, source, frozenset({ContentScope.METADATA})).documents == ()


def test_the_selection_is_followed_and_never_chosen(service, tmp_path):
    _, source = source_of(tmp_path)
    selected = extract(service, source, selection=("OEBPS/text/two words.xhtml", "OEBPS/content.opf"))
    assert [document.logical_key for document in selected.documents] == ["OEBPS/content.opf", "OEBPS/text/two words.xhtml"]
    assert extract(service, source, selection=("OEBPS/nothing.xhtml",)).documents == ()


def test_a_selected_navigation_needs_its_scope(service, tmp_path):
    _, source = source_of(tmp_path)
    assert service.extract("epub", source, ExtractionRequest(PROJECT, selection=("OEBPS/nav.xhtml",))).documents == ()


def test_html_is_delegated_and_units_keep_markup_protected(service, tmp_path):
    _, source = source_of(tmp_path)
    unit = service.extract("epub", source, ExtractionRequest(PROJECT)).documents[0].units[1]
    assert html_unit_template(unit) == "Hello __AYVU_TAG_0__world__AYVU_TAG_1__"
    assert unit.content.locator.schema == "html_position"


def test_extraction_agrees_with_the_chapter_order_of_the_current_flow(service, tmp_path):
    from ebooklib import epub as ebooklib_epub

    path, source = source_of(tmp_path, epub_bytes(replace=opf_with(b'<item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>', b"")))
    book = ebooklib_epub.read_epub(str(path))
    with ZipFile(path) as archive:
        current = [item.archive_path for item in epub_io._translation_documents(book, archive, "OEBPS/content.opf", epub_io._opf_base_path("OEBPS/content.opf"), False)]
    assert [document.logical_key for document in service.extract("epub", source, ExtractionRequest(PROJECT)).documents] == current


def test_a_document_that_declares_entities_is_refused(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": b'<!DOCTYPE x [<!ENTITY e "v">]><html><body><p>&e;</p></body></html>'}))
    assert extract(service, source).failure.code == "entity_declaration"


# Reconstruction: what changes and what does not

def test_only_translated_members_change_and_every_other_keeps_its_bytes(service, tmp_path):
    path, source = source_of(tmp_path, epub_bytes(comment=b"a comment"))
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, done(documents, lambda text: text.replace("Second", "Segundo").replace("Hello", "Ola")))
    assert result.failure is None
    output = env.read(publication)
    before, after = members_of(path.read_bytes()), members_of(output)
    assert list(before) == list(after)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"}
    with ZipFile(io.BytesIO(output)) as archive, ZipFile(path) as original:
        assert archive.comment == b"a comment"
        for left, right in zip(original.infolist(), archive.infolist()):
            assert (left.filename, left.compress_type, left.date_time, left.external_attr) == (right.filename, right.compress_type, right.date_time, right.external_attr)
    assert output[30:58] == b"mimetypeapplication/epub+zip" and int.from_bytes(output[8:10], "little") == ZIP_STORED


def test_a_translated_document_keeps_namespaces_links_and_images(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = extract(service, source).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, done(documents, lambda text: text.replace("world", "mundo").replace("A picture", "Uma imagem")))
    assert result.failure is None
    soup = BeautifulSoup(members_of(env.read(publication))["OEBPS/text/one.xhtml"], "lxml-xml")
    link = soup.find("a")
    assert (link["epub:type"], link["href"], link.string) == ("noteref", "two%20words.xhtml#n", "mundo")
    assert (soup.find("img")["src"], soup.find("img")["alt"]) == ("../images/a.png", "Uma imagem")
    assert soup.find("link")["href"] == "../style.css"


def test_translated_text_never_becomes_markup_in_the_book(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, done(documents, lambda text: text.replace("Second", "<script>x()</script>")))
    assert result.failure is None
    soup = BeautifulSoup(members_of(env.read(publication))["OEBPS/text/two words.xhtml"], "lxml-xml")
    assert soup.find("script") is None and "<script>" in soup.find("p").get_text()


def test_a_title_change_touches_only_the_title(service, tmp_path):
    path, source = source_of(tmp_path)
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "O <Titulo> & cia"),))
    assert result.failure is None
    after = members_of(env.read(publication))["OEBPS/content.opf"]
    tree = etree.fromstring(after)
    title = tree.find(".//{http://purl.org/dc/elements/1.1/}title")
    assert title.text == "O <Titulo> & cia" and title.get("id") == "t" and len(title) == 0
    creator = tree.find(".//{http://purl.org/dc/elements/1.1/}creator")
    assert creator.get("{http://www.idpf.org/2007/opf}role") == "aut"
    assert b"<!-- keep -->" in after and b"opf:role" in after
    title.text = "The Title"
    assert etree.tostring(tree, method="c14n") == etree.tostring(etree.fromstring(OPF), method="c14n")
    members = members_of(path.read_bytes())
    assert after.startswith(b"<?xml") and {name for name, value in members_of(env.read(publication)).items() if members[name] != value} == {"OEBPS/content.opf"}


def test_no_title_translation_leaves_the_package_byte_identical(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = extract(service, source).documents
    translations = tuple(item for item in done(documents) if item.text != "The Title")
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, translations, kind=PreviewRequest)
    assert result.failure is None and members_of(env.read(publication))["OEBPS/content.opf"] == OPF


def test_a_title_that_changed_is_stale(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    from ayvu.content import build_document, Locator, SemanticKind, UnitContent, UNKNOWN_LOCALE

    other = build_document(PROJECT, "OEBPS/content.opf", epub_format.EPUB_ADAPTER, UNKNOWN_LOCALE,
                           [UnitContent(Locator("epub_package", 1, "title"), "Another Title", SemanticKind("title"), (0,))],
                           source_fingerprint=documents[0].source_fingerprint)
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, (other,), (UnitTranslation(other.units[0].unit_id, "x"),))
    assert result.failure.code == "stale_revision" and not env.is_ready(publication)


def test_a_literal_token_in_the_title_comes_back(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace=opf_with(b">The Title<", b">__AYVU_TAG_3__ Title<")))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    unit = documents[0].units[0]
    assert html_unit_template(unit) == "__AYVU_TAG_0__ Title"
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(unit.unit_id, "__AYVU_TAG_0__ Titulo"),))
    assert result.failure is None and b">__AYVU_TAG_3__ Titulo<" in members_of(env.read(publication))["OEBPS/content.opf"]


def test_a_preview_can_translate_one_document_and_leave_the_rest_identical(service, tmp_path):
    path, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    first = documents[0]
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(first.units[0].unit_id, "Parcial"),), kind=PreviewRequest)
    assert result.failure is None
    after = members_of(env.read(publication))
    assert after["OEBPS/text/two words.xhtml"] == TWO and b"Parcial" in after["OEBPS/text/one.xhtml"]


def test_a_preview_with_no_results_keeps_every_member(service, tmp_path):
    path, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (), kind=PreviewRequest)
    assert result.failure is None and members_of(env.read(publication)) == members_of(path.read_bytes())


def test_reconstructing_a_selection_leaves_other_documents_identical(service, tmp_path):
    path, source = source_of(tmp_path)
    selected = extract(service, source, frozenset({ContentScope.BODY}), selection=("OEBPS/text/one.xhtml",)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, selected)
    assert result.failure is None and members_of(env.read(publication))["OEBPS/text/two words.xhtml"] == TWO


def test_a_token_error_in_any_document_publishes_nothing(service, tmp_path):
    path, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    good = list(done(documents))
    bad = next(index for index, unit in enumerate(all_units(documents)) if "__AYVU_TAG_0__" in html_unit_template(unit))
    good[bad] = UnitTranslation(good[bad].unit_id, "tokens are gone")
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, tuple(good))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTEGRITY, "missing_token") and not env.is_ready(publication)
    assert path.read_bytes() == epub_bytes()


def test_a_book_without_documents_is_not_valid_output(tmp_path):
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    service = FormatService(registry)
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": OPF.replace(b'media-type="application/xhtml+xml"', b'media-type="text/plain"')}))
    title = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, title, (UnitTranslation(title[0].units[0].unit_id, "T"),))
    assert result.failure.code == "invalid_output" and not env.is_ready(publication)


def test_output_that_is_not_a_valid_epub_is_never_published(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    real = epub_format.EpubArchive
    calls = []

    def second_refuses(*args, **options):
        calls.append(1)
        if len(calls) == 2:
            raise EpubArchiveError("invalid_mimetype")
        return real(*args, **options)

    monkeypatch.setattr(epub_format, "EpubArchive", second_refuses)
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents)
    assert result.failure.code == "invalid_output" and not env.is_ready(publication)


def test_nothing_reaches_staging_before_the_book_is_checked(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    monkeypatch.setattr(epub_format, "_findings", lambda *_args: (_ for _ in ()).throw(EpubArchiveError("invalid_mimetype")))
    env = FormatEnv(tmp_path / "env")
    result, _ = rebuild(service, env, source, documents)
    assert result.failure.code == "invalid_output"
    assert [path for path in (tmp_path / "env" / "artifacts").rglob("*") if path.is_file() and path.stat().st_size] == []


# Validation

def test_a_clean_book_has_no_findings(service, tmp_path):
    _, source = source_of(tmp_path)
    result = service.validate("epub", source)
    assert result.ok and result.findings == ()


@pytest.mark.parametrize("body,code,count", [
    ('<p>x</p><a href="gone.xhtml">y</a><a href="gone2.xhtml">z</a>', "broken_internal_link", 2),
    ('<p>x</p><img src="gone.png"/>', "missing_image", 1),
    ('<p>x</p><svg xmlns="http://www.w3.org/2000/svg"><image href="gone.png"/></svg>', "missing_image", 1),
    ("", "empty_chapter", 1),
], ids=range(4))
def test_each_problem_is_a_counted_warning(service, tmp_path, body, code, count):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": XHTML.format(body).encode()}))
    findings = service.validate("epub", source).findings
    assert [(item.severity, item.code, item.count) for item in findings if item.code == code] == [(FindingSeverity.WARNING, code, count)]
    assert service.validate("epub", source).ok


def test_escaped_links_and_external_links_are_not_broken(service, tmp_path):
    body = '<p>x</p><a href="two%20words.xhtml#n">a</a><a href="https://example.test">b</a><a href="#n">c</a><img src="../images/a.png"/>'
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": XHTML.format(body).encode()}))
    assert service.validate("epub", source).findings == ()


def test_an_image_only_chapter_is_not_empty(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": XHTML.format('<img src="../images/a.png"/>').encode()}))
    assert service.validate("epub", source).findings == ()


def test_an_empty_navigation_is_not_an_empty_chapter(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/nav.xhtml": XHTML.format("").encode()}))
    assert service.validate("epub", source).findings == ()


def test_no_documents_is_an_error(service, tmp_path):
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": OPF.replace(b'media-type="application/xhtml+xml"', b'media-type="text/plain"')}))
    result = service.validate("epub", source)
    assert not result.ok and [item.code for item in result.findings] == ["no_documents"]


# Publication and crashes

def test_the_output_store_refuses_a_real_alias_before_any_change(service, tmp_path):
    path, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    alias = tmp_path / "alias.epub"
    alias.symlink_to(path)
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, guard=LocalOutputGuard(alias))
    assert result.failure.code == "output_is_source" and env.publisher.calls == 0 and path.read_bytes() == epub_bytes()


def crash_at(env, stage):
    """Makes one step of publication fail, as a process dying at that moment would leave things."""
    service = env.artifacts
    if stage == "rename":
        original = service._payloads.publish

        def failing(*_args):
            raise ArtifactStoreError("crashed")
        service._payloads.publish = failing
        return lambda: setattr(service._payloads, "publish", original)
    original = service._repository.mark_ready

    def failing(*_args):
        raise ArtifactStoreError("crashed")
    service._repository.mark_ready = failing
    return lambda: setattr(service._repository, "mark_ready", original)


def test_a_crash_after_the_rename_is_not_success_and_reconciles_to_ready(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    restore = crash_at(env, "commit")
    result, publication = rebuild(service, env, source, documents)
    restore()
    assert result.artifact is None and result.failure.code == "crashed"
    assert not env.is_ready(publication)
    # While the job is alive nothing is decided, because it may still finish the work itself.
    assert len(env.artifacts.reconcile().active) == 1 and not env.is_ready(publication)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    report = env.artifacts.reconcile()
    assert (len(report.completed), report.abandoned) == (1, ())
    assert env.is_ready(publication)
    assert members_of(env.read(publication))["OEBPS/style.css"] == b"p { color: red }"


def test_a_crash_before_the_rename_never_becomes_ready(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    restore = crash_at(env, "rename")
    result, publication = rebuild(service, env, source, documents)
    restore()
    assert result.artifact is None and result.failure.code == "crashed" and not env.is_ready(publication)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    report = env.artifacts.reconcile()
    assert (report.completed, len(report.abandoned)) == ((), 1) and not env.is_ready(publication)


def test_a_failing_validation_leaves_no_ready_artifact_after_reconciliation(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    monkeypatch.setattr(epub_format, "_findings", lambda *_args: (_ for _ in ()).throw(RuntimeError("Call me Ishmael")))
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents)
    assert result.failure.code == "adapter_failed" and "Ishmael" not in repr(result)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    env.artifacts.reconcile()
    assert not env.is_ready(publication)


def test_the_source_is_unchanged_after_success_and_after_every_kind_of_failure(service, tmp_path):
    path, source = source_of(tmp_path)
    fingerprint = source.fingerprint()
    documents = extract(service, source).documents
    env = FormatEnv(tmp_path / "env")
    rebuild(service, env, source, documents)
    rebuild(service, env, source, documents, ())
    for point in format_safe_points(epub_format_adapter().descriptor):
        token = TriggerToken(format_safe_points(epub_format_adapter().descriptor), point)
        service.reconstruct("epub", source, ReconstructionRequest(documents, done(documents)), env.publication(), cancellation=token)
    assert source.fingerprint() == fingerprint and path.read_bytes() == epub_bytes()


# The spool

def test_a_large_book_spills_and_comes_back_whole(service, tmp_path, monkeypatch):
    monkeypatch.setattr(epub_format, "SPOOL_MEMORY_BYTES", 16)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents)
    assert result.failure is None and members_of(env.read(publication))["OEBPS/style.css"] == b"p { color: red }"


def test_output_chunks_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_format, "_CHUNK_BYTES", 10)
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    service = FormatService(registry)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    sizes = []
    inner = env.publisher.publish

    def spying(request, chunks, **options):
        def counted():
            for chunk in chunks:
                sizes.append(len(chunk))
                yield chunk
        return inner(request, counted(), **options)

    env.publisher.publish = spying
    result, _ = rebuild(service, env, source, documents)
    assert result.failure is None and max(sizes) <= 10 and len(sizes) > 1


# Privacy and boundaries

def test_nothing_from_the_book_appears_in_a_result_or_a_representation(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = extract(service, source).documents
    inventory = service.inspect("epub", source)
    for value in (inventory, inventory.inventory, documents[1], documents[1].units[0], epub_format_adapter(), epub_format_adapter().inspector):
        assert not any(secret in repr(value) for secret in ("Hello", "The Title", "An Author", "text/one"))


def test_the_adapter_imports_no_provider_cache_glossary_or_interface():
    import ast
    import subprocess
    import sys

    forbidden = ("ayvu.cache", "ayvu.glossary", "ayvu.translator", "ayvu.translation_memory", "ayvu.translation_pipeline", "ayvu.provider", "ayvu.cli", "ayvu.epub_io",
                 "ayvu.html_translate", "ayvu.validation", "ebooklib", "typer", "rich", "requests", "sqlite3")
    source = (Path(__file__).parents[1] / "src" / "ayvu" / "epub_format.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            name = f"ayvu.{node.module}" if node.level else node.module
            assert not name.startswith(forbidden), name
    code = f"import sys, ayvu.epub_format; print([m for m in sys.modules if m.startswith({forbidden!r})])"
    done_ = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done_.stdout.strip() == "[]"


def test_no_html_is_traversed_here():
    source = (Path(__file__).parents[1] / "src" / "ayvu" / "epub_format.py").read_text()
    assert ".children" not in source and "NavigableString" not in source and "get_text" not in source


def test_the_current_flow_writes_no_durable_artifact(tmp_path):
    source = (Path(__file__).parents[1] / "src" / "ayvu" / "epub_io.py").read_text()
    assert "epub_format" not in source and "ArtifactService" not in source and "artifact" not in source.lower()


def test_a_document_title_is_its_first_visible_text_with_tidy_spaces(service, tmp_path):
    body = "<p>\n   First\n   line  </p><p>Second</p>"
    html = f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>'.encode()
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/text/one.xhtml": html}))
    titles = {item.logical_key: item.title for item in service.inspect("epub", source).inventory.documents}
    assert titles["OEBPS/text/one.xhtml"] == "First line"


def test_a_comment_outside_the_package_element_survives_a_title_change(service, tmp_path):
    outside = OPF.replace(b"<package ", b"<!-- before the package -->\n<package ", 1)
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": outside}))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "Titulo"),))
    assert result.failure is None and b"<!-- before the package -->" in members_of(env.read(publication))["OEBPS/content.opf"]


def test_a_comment_before_the_metadata_does_not_stop_a_title_change(service, tmp_path):
    changed = OPF.replace(b"<metadata>", b"<!-- inside the package --><metadata>", 1)
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": changed}))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "Titulo"),))
    written = members_of(env.read(publication))["OEBPS/content.opf"]
    assert result.failure is None and b"<!-- inside the package -->" in written and b">Titulo</dc:title>" in written


def test_a_title_change_keeps_cdata_elsewhere_in_the_package(service, tmp_path):
    changed = OPF.replace(b"<!-- keep -->", b"<dc:description><![CDATA[<p>x & y</p>]]></dc:description>", 1)
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": changed}))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "Titulo"),))
    assert result.failure is None and b"<![CDATA[<p>x & y</p>]]>" in members_of(env.read(publication))["OEBPS/content.opf"]


@pytest.mark.parametrize("title", [b"The<!-- c --> Title", b"The <span>Title</span>", b"The<?keep this?> Title"])
def test_a_title_holding_other_nodes_is_not_offered_for_translation(service, tmp_path, title):
    changed = OPF.replace(b"The Title", title, 1)
    _, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": changed}))
    assert extract(service, source, frozenset({ContentScope.METADATA})).documents == ()


def test_the_adapter_and_the_current_flow_write_the_same_package(service, tmp_path):
    # ebooklib, which the current flow opens every book with, rejects a remote manifest item.
    openable = OPF.replace(b'    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>\n', b"")
    path, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": openable}))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "PT:The Title"),))
    assert result.failure is None

    class Prefix:
        def translate(self, text, source, target):
            return f"PT:{text}"

    output = tmp_path / "current.epub"
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"), translate_metadata=True)
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        epub_io.translate_epub(path, output, translator=Prefix(), cache=cache, options=options)
    assert members_of(output.read_bytes())["OEBPS/content.opf"] == members_of(env.read(publication))["OEBPS/content.opf"]


def test_cancelling_between_documents_stops_before_any_document_is_rebuilt(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    rebuilt = []
    monkeypatch.setattr(epub_format, "reconstruct_html", lambda *_args, **_options: rebuilt.append(1) or b"<html/>")
    token = TriggerToken(format_safe_points(epub_format_adapter().descriptor), epub_format.BETWEEN_DOCUMENTS)
    env = FormatEnv(tmp_path / "env")
    result = service.reconstruct("epub", source, ReconstructionRequest(documents, done(documents)), env.publication(), cancellation=token)
    assert result.failure.family is JobFailureFamily.CANCELLED and rebuilt == []


def test_the_descriptor_states_the_source_limit():
    assert epub_format_adapter().descriptor.limits.max_source_bytes == 8 * 1024 ** 3


def test_a_small_book_is_built_in_memory(service, tmp_path, monkeypatch):
    sizes = []
    real = epub_format.tempfile.SpooledTemporaryFile

    def spying(*args, max_size, **options):
        sizes.append(max_size)
        return real(*args, max_size=max_size, **options)

    monkeypatch.setattr(epub_format.tempfile, "SpooledTemporaryFile", spying)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    result, _ = rebuild(service, FormatEnv(tmp_path / "env"), source, documents)
    assert result.failure is None and sizes == [64 * 1024 ** 2]
