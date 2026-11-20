import io
from zipfile import ZIP_STORED, ZipFile

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from ayvu.formats.epub import adapter as epub_format
from ayvu.formats.epub.archive import EpubArchiveError
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import ContentScope, ExtractionRequest, PreviewRequest, UnitTranslation
from ayvu.formats.service import FormatRegistry, FormatService
from ayvu.formats.epub.html_structure import html_unit_template
from tests.support.epub_fakes import OPF, TWO, epub_bytes, members, opf_with
from tests.support.format_contract_env import PROJECT, FormatEnv
from ayvu.kernel import LanguagePair, TranslationOptions, JobFailureFamily

from tests.formats.epub.epub_format_support import all_units, done, extract
from tests.formats.epub.epub_format_support import members_of, rebuild, service, source_of


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
    from ayvu.content.model import build_document, Locator, SemanticKind, UnitContent, UNKNOWN_LOCALE

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


def test_the_adapter_rewrites_only_the_title_of_the_package(service, tmp_path):
    """The package the retired v1 flow wrote for the same book, pinned when it was removed (REF-18 step 6)."""
    openable = OPF.replace(b'    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>\n', b"")
    path, source = source_of(tmp_path, epub_bytes(replace={"OEBPS/content.opf": openable}))
    documents = extract(service, source, frozenset({ContentScope.METADATA})).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents, (UnitTranslation(documents[0].units[0].unit_id, "PT:The Title"),))
    assert result.failure is None

    expected = openable.replace(b'<?xml version="1.0" encoding="utf-8"?>', b"<?xml version='1.0' encoding='utf-8'?>", 1)
    assert members_of(env.read(publication))["OEBPS/content.opf"] == expected.replace(b">The Title<", b">PT:The Title<", 1)
