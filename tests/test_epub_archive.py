import io
import re
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
from ebooklib import epub

from ayvu import epub_io
from ayvu.epub_archive import DEFAULT_LIMITS, EpubArchive, EpubArchiveError, EpubLimits, declares_entities, resolve_internal_path
from epub_fakes import CONTAINER, OPF, epub_bytes, members, opf_with, zip_bytes


def opened(data, **options):
    return EpubArchive(io.BytesIO(data), **options)


def refused(data, code, **options):
    with pytest.raises(EpubArchiveError, match=f"^{code}$"):
        opened(data, **options)


def test_a_valid_epub_opens_and_states_its_structure():
    with opened(epub_bytes()) as archive:
        package = archive.package
        assert package.archive_path == "OEBPS/content.opf" and package.base == "OEBPS" and package.version == "3.0"
        assert package.title == "The Title" and package.creators == ("An Author", "Another") and package.language == "en"
        assert [item.archive_path for item in package.documents] == ["OEBPS/nav.xhtml", "OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"]
        assert [item.is_navigation for item in package.documents] == [True, False, False]
        assert package.spine == ("c1", "c2")
        assert archive.names()[0] == "mimetype"


def test_a_package_at_the_root_of_the_archive_has_no_base():
    moved = zip_bytes([("mimetype", b"application/epub+zip", ZIP_STORED),
                       ("META-INF/container.xml", CONTAINER.replace(b"OEBPS/content.opf", b"p.opf"), ZIP_DEFLATED),
                       ("p.opf", b'<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest><spine/></package>', ZIP_DEFLATED),
                       ("a.xhtml", b"<html/>", ZIP_DEFLATED)])
    with opened(moved) as archive:
        assert archive.package.base == "" and archive.package.documents[0].archive_path == "a.xhtml"


def test_a_manifest_href_can_go_up_but_not_out():
    data = zip_bytes([("mimetype", b"application/epub+zip", ZIP_STORED), ("META-INF/container.xml", CONTAINER, ZIP_DEFLATED),
                      ("OEBPS/content.opf", b'<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest><item id="a" href="../Text/a.xhtml?x=1#f" media-type="application/xhtml+xml"/></manifest><spine/></package>', ZIP_DEFLATED),
                      ("Text/a.xhtml", b"<html/>", ZIP_DEFLATED)])
    with opened(data) as archive:
        assert archive.package.documents[0].archive_path == "Text/a.xhtml"


def test_only_a_document_can_be_the_navigation():
    with opened(epub_bytes(replace=opf_with(b'media-type="text/css"', b'media-type="text/css" properties="nav"'))) as archive:
        item = next(item for item in archive.package.items if item.item_id == "css")
        assert item.properties == ("nav",) and not item.is_navigation


def test_a_member_over_the_bound_is_not_even_opened(monkeypatch):
    with opened(epub_bytes()) as archive:
        monkeypatch.setattr(archive._zip, "open", lambda *_args: (_ for _ in ()).throw(AssertionError("opened")))
        with pytest.raises(EpubArchiveError, match="^document_too_large$"):
            archive.read("OEBPS/style.css", 5)


def test_remote_manifest_items_are_not_members():
    with opened(epub_bytes()) as archive:
        remote = next(item for item in archive.package.items if item.item_id == "remote")
        assert remote.archive_path is None and remote not in archive.package.documents


@pytest.mark.parametrize("name", [
    "../evil", "a/../../evil", "/abs", "C:evil", "c:\\evil", "a\\b", "a//b", "./a", "a/./b", "a/\x01b", "a\x7fb",
], ids=range(11))
def test_unsafe_member_names_are_refused(name):
    refused(epub_bytes(add=[(name, b"x", ZIP_STORED)]), "unsafe_member_name")


def test_a_nul_in_a_member_name_is_refused():
    raw = epub_bytes(add=[("OEBPS/aXb.css", b"x", ZIP_STORED)])
    assert raw.count(b"OEBPS/aXb.css") == 2
    refused(raw.replace(b"OEBPS/aXb.css", b"OEBPS/a\0b.css"), "unsafe_member_name")


def test_a_directory_entry_is_allowed():
    with opened(epub_bytes(add=[("OEBPS/extra/", b"", ZIP_STORED)])) as archive:
        assert "OEBPS/extra/" in archive.names()


@pytest.mark.parametrize("name", ["OEBPS/style.css", "oebps/STYLE.css", "OEBPS/style.css/"], ids=["same", "case", "slash"])
def test_duplicate_canonical_names_are_refused(name):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = epub_bytes(add=[(name, b"x", ZIP_STORED)])
    refused(data, "duplicate_member")


def test_names_differing_only_by_unicode_form_are_duplicates():
    refused(epub_bytes(add=[("OEBPS/caf\u00e9.css", b"x", ZIP_STORED), ("OEBPS/cafe\u0301.css", b"y", ZIP_STORED)]), "duplicate_member")


def test_an_encrypted_member_is_refused():
    raw = bytearray(epub_bytes())
    # Set the encryption bit on the second member, in the central directory.
    offset = raw.index(b"PK\x01\x02", raw.index(b"PK\x01\x02") + 1)
    raw[offset + 8] |= 1
    refused(bytes(raw), "encrypted_member")


def test_an_unsupported_compression_is_refused():
    refused(epub_bytes(add=[("OEBPS/x.bin", b"x" * 100, ZIP_BZIP2)]), "unsupported_compression")


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04broken"], ids=range(3))
def test_what_is_not_a_zip_is_corrupt(data):
    refused(data, "corrupt_archive")


@pytest.mark.parametrize("limits,code", [
    (EpubLimits(max_source_bytes=10), "source_too_large"),
    (EpubLimits(max_members=3), "too_many_members"),
    (EpubLimits(max_member_bytes=100), "member_too_large"),
    (EpubLimits(max_uncompressed_bytes=100), "archive_too_large"),
    (EpubLimits(max_name_chars=5), "member_name_too_long"),
], ids=lambda value: value if isinstance(value, str) else None)
def test_every_budget_is_enforced(limits, code):
    refused(epub_bytes(), code, limits=limits)


def test_a_suspicious_compression_ratio_is_refused_before_reading():
    bomb = epub_bytes(add=[("OEBPS/bomb.bin", b"\0" * (3 * 1024 * 1024), ZIP_DEFLATED)])
    refused(bomb, "suspicious_compression")
    # Below the threshold, a highly compressible member is ordinary.
    with opened(epub_bytes(add=[("OEBPS/small.bin", b"\0" * (1024 * 1024), ZIP_DEFLATED)])):
        pass


def test_a_stored_large_member_is_not_suspicious():
    with opened(epub_bytes(add=[("OEBPS/big.bin", b"\0" * (2 * 1024 * 1024), ZIP_STORED)])):
        pass


def test_the_default_limits_are_the_documented_ones():
    assert (DEFAULT_LIMITS.max_source_bytes, DEFAULT_LIMITS.max_uncompressed_bytes, DEFAULT_LIMITS.max_member_bytes) == (8 * 1024 ** 3, 4 * 1024 ** 3, 1024 ** 3)
    assert (DEFAULT_LIMITS.max_members, DEFAULT_LIMITS.max_document_bytes, DEFAULT_LIMITS.max_package_bytes) == (20000, 64 * 1024 ** 2, 16 * 1024 ** 2)
    assert (DEFAULT_LIMITS.max_compression_ratio, DEFAULT_LIMITS.ratio_applies_above_bytes, DEFAULT_LIMITS.max_name_chars) == (200, 1024 ** 2, 1024)


def test_reading_stops_at_the_bound():
    with opened(epub_bytes()) as archive:
        assert archive.read("OEBPS/style.css", 100) == b"p { color: red }"
        with pytest.raises(EpubArchiveError, match="^document_too_large$"):
            archive.read("OEBPS/style.css", 5)
    with opened(epub_bytes(), limits=EpubLimits(max_document_bytes=5, max_package_bytes=100000)) as archive:
        with pytest.raises(EpubArchiveError, match="^document_too_large$"):
            archive.read_document("OEBPS/style.css")


def test_a_header_that_lies_about_size_cannot_pass_the_bound():
    raw = bytearray(epub_bytes())
    # Claim the stylesheet is small, in the central directory only.
    name = b"OEBPS/style.css"
    offset = raw.index(b"PK\x01\x02" , raw.index(b"PK\x01\x02"))
    while raw[offset + 46:offset + 46 + len(name)] != name:
        offset = raw.index(b"PK\x01\x02", offset + 4)
    raw[offset + 24:offset + 28] = (4).to_bytes(4, "little")
    with opened(bytes(raw)) as archive:
        with pytest.raises(EpubArchiveError, match="^corrupt_member$"):
            archive.read("OEBPS/style.css")


def test_a_missing_member_is_a_code():
    with opened(epub_bytes()) as archive:
        with pytest.raises(EpubArchiveError, match="^missing_member$"):
            archive.read("nothing")


def test_a_corrupted_member_is_a_code():
    raw = bytearray(epub_bytes())
    position = raw.index(b"PK\x01\x02")
    # Damage compressed bytes of the second member, leaving the structure readable.
    local = raw.index(b"PK\x03\x04", 10)
    start = local + 30 + len("META-INF/container.xml")
    raw[start:start + 8] = b"\xff" * 8
    assert position > start
    refused(bytes(raw), "corrupt_member")


@pytest.mark.parametrize("build,code", [
    (lambda: epub_bytes(drop=("META-INF/container.xml",)), "missing_container"),
    (lambda: epub_bytes(replace={"META-INF/container.xml": b"<not-xml"}), "malformed_container"),
    (lambda: epub_bytes(replace={"META-INF/container.xml": b"<container/>"}), "malformed_container"),
    (lambda: epub_bytes(replace={"META-INF/container.xml": CONTAINER.replace(b"OEBPS/content.opf", b"../x.opf")}), "malformed_container"),
    (lambda: epub_bytes(drop=("OEBPS/content.opf",)), "missing_package"),
    (lambda: epub_bytes(replace={"OEBPS/content.opf": b"<broken"}), "malformed_package"),
    (lambda: epub_bytes(replace={"OEBPS/content.opf": b"<html/>"}), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b"<spine>", b"<!-- x --><other>") | {}) , "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'idref="c1"', b'idref="nothing"')), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'id="c2"', b'id="c1"')), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href=""')), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'id="css"', b'id="img"')), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'id="css" ', b'')), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'media-type="text/css"', b'')), "malformed_package"),
    (lambda: epub_bytes(replace={"OEBPS/content.opf": OPF.replace(b"<package ", b"<other ").replace(b"</package>", b"</other>")}), "malformed_package"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href="nav.xhtml"')), "malformed_package"),
    (lambda: epub_bytes(drop=("OEBPS/style.css",)), "manifest_member_missing"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href="../../../etc/passwd"')), "unsafe_manifest_href"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href="/etc/passwd"')), "unsafe_manifest_href"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href="a\\b.css"')), "unsafe_manifest_href"),
    (lambda: epub_bytes(replace=opf_with(b'href="style.css"', b'href="..%2F..%2F..%2Fetc%2Fpasswd"')), "unsafe_manifest_href"),
], ids=range(21))
def test_a_broken_structure_is_a_structured_failure(build, code):
    refused(build(), code)


@pytest.mark.parametrize("member", ["META-INF/container.xml", "OEBPS/content.opf"])
def test_xml_that_declares_entities_is_refused(member):
    original = dict((name, data) for name, data, _ in members())[member]
    declared = b'<!DOCTYPE x [<!ENTITY e "boom">]>' + original.split(b"?>", 1)[-1]
    refused(epub_bytes(replace={member: declared}), "entity_declaration")


def test_a_document_that_declares_entities_is_refused_when_read():
    document = b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><html><body>&e;</body></html>'
    with opened(epub_bytes(replace={"OEBPS/text/one.xhtml": document})) as archive:
        with pytest.raises(EpubArchiveError, match="^entity_declaration$"):
            archive.read_document("OEBPS/text/one.xhtml")
        assert archive.read_document("OEBPS/text/two words.xhtml")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
def test_entity_declarations_are_found_in_every_encoding(encoding):
    assert declares_entities('<!DOCTYPE x [<!ENTITY e "v">]>'.encode(encoding))
    assert not declares_entities("<!DOCTYPE html><html/>".encode(encoding))


def test_an_ordinary_doctype_is_allowed():
    declared = b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"><html/>'
    with opened(epub_bytes(replace={"OEBPS/text/one.xhtml": declared})) as archive:
        assert archive.read_document("OEBPS/text/one.xhtml") == declared


def encryption(uri, algorithm):
    return (f'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
            f'<enc:EncryptionMethod Algorithm="{algorithm}"/><enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData></enc:EncryptedData></encryption>').encode()


@pytest.mark.parametrize("uri,algorithm", [
    ("OEBPS/text/one.xhtml", "http://www.w3.org/2001/04/xmlenc#aes256-cbc"),
    ("OEBPS/text/one.xhtml", "http://www.idpf.org/2008/embedding"),
    ("OEBPS/content.opf", "http://www.idpf.org/2008/embedding"),
    ("OEBPS/images/a.png", "http://www.w3.org/2001/04/xmlenc#aes256-cbc"),
    ("OEBPS/text/two%20words.xhtml", "http://www.idpf.org/2008/embedding"),
], ids=range(5))
def test_protected_content_is_refused(uri, algorithm):
    refused(epub_bytes(add=[("META-INF/encryption.xml", encryption(uri, algorithm), ZIP_DEFLATED)]), "encrypted_content")


@pytest.mark.parametrize("algorithm", ["http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"])
def test_obfuscated_fonts_are_not_protected_content(algorithm):
    data = epub_bytes(add=[("OEBPS/f.otf", b"font", ZIP_STORED), ("META-INF/encryption.xml", encryption("OEBPS/f.otf", algorithm), ZIP_DEFLATED)])
    with opened(data):
        pass


@pytest.mark.parametrize("content", [b"<broken", b'<encryption xmlns:e="http://www.w3.org/2001/04/xmlenc#"><e:EncryptedData/></encryption>'], ids=range(2))
def test_a_malformed_encryption_file_is_refused(content):
    refused(epub_bytes(add=[("META-INF/encryption.xml", content, ZIP_DEFLATED)]), "malformed_encryption")


def test_opening_checks_each_member_at_a_checkpoint():
    calls = []
    with opened(epub_bytes(), checkpoint=lambda: calls.append(1)):
        pass
    assert len(calls) == len(members())


def test_a_checkpoint_can_stop_the_opening():
    class Stop(Exception):
        pass

    def stop():
        raise Stop

    with pytest.raises(Stop):
        opened(epub_bytes(), checkpoint=stop)


# Copying

def copied(data, replacements, **options):
    output = io.BytesIO()
    with opened(data) as archive:
        archive.copy_to(output, replacements, **options)
    return output.getvalue()


def test_copying_keeps_every_member_as_it_was():
    data = epub_bytes(comment=b"archive comment", add=[("OEBPS/extra/", b"", ZIP_STORED)])
    with ZipFile(io.BytesIO(data)) as source, ZipFile(io.BytesIO(copied(data, {}))) as output:
        assert output.comment == b"archive comment" and output.namelist() == source.namelist()
        for before, after in zip(source.infolist(), output.infolist()):
            assert (before.filename, before.compress_type, before.date_time, before.external_attr, before.create_system, before.comment) == (
                after.filename, after.compress_type, after.date_time, after.external_attr, after.create_system, after.comment)
            assert source.read(before) == output.read(after)


def test_copying_replaces_only_what_was_named():
    data = epub_bytes()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(io.BytesIO(copied(data, {"OEBPS/text/one.xhtml": b"NEW"}))) as output:
        for info in source.infolist():
            expected = b"NEW" if info.filename == "OEBPS/text/one.xhtml" else source.read(info)
            assert output.read(info.filename) == expected
        replaced = output.getinfo("OEBPS/text/one.xhtml")
        assert replaced.compress_type == ZIP_DEFLATED and replaced.date_time == source.getinfo("OEBPS/text/one.xhtml").date_time


def test_the_copy_is_itself_a_valid_epub_with_a_stored_first_mimetype():
    output = copied(epub_bytes(), {"OEBPS/text/one.xhtml": b"<html/>"})
    with opened(output) as archive:
        assert archive.names()[0] == "mimetype"
    assert output[:4] == b"PK\x03\x04" and output[30:38] == b"mimetype" and output[38:58] == b"application/epub+zip"
    assert int.from_bytes(output[6:8], "little") & 0x8 == 0


@pytest.mark.parametrize("name", ["mimetype", "META-INF/container.xml"])
def test_structure_members_cannot_be_replaced(name):
    with pytest.raises(EpubArchiveError, match="^protected_member$"):
        copied(epub_bytes(), {name: b"x"})


def test_an_unknown_replacement_is_refused():
    with pytest.raises(EpubArchiveError, match="^unknown_replacement$"):
        copied(epub_bytes(), {"OEBPS/new.xhtml": b"x"})


def test_copying_does_not_change_the_open_archive():
    data = epub_bytes()
    with opened(data) as archive:
        before = [(info.filename, info.header_offset, info.CRC, info.compress_size) for info in archive.infos()]
        archive.copy_to(io.BytesIO(), {"OEBPS/text/one.xhtml": b"NEW"})
        assert before == [(info.filename, info.header_offset, info.CRC, info.compress_size) for info in archive.infos()]
        assert archive.read("OEBPS/text/one.xhtml") != b"NEW"


def test_copying_checks_between_members():
    calls = []
    copied(epub_bytes(), {}, checkpoint=lambda: calls.append(1))
    assert len(calls) == len(members())


def test_the_source_stream_is_only_read():
    class ReadOnly(io.BytesIO):
        def write(self, *_args):
            raise AssertionError("the source was written")

        def truncate(self, *_args):
            raise AssertionError("the source was truncated")

    source = ReadOnly(epub_bytes())
    with EpubArchive(source) as archive:
        archive.copy_to(io.BytesIO(), {})


# Agreement with the current flow

def legacy_documents(path):
    book = epub.read_epub(str(path))
    opf = epub_io._get_opf_archive_path(path)
    with ZipFile(path) as archive:
        base = epub_io._opf_base_path(opf)
        return ([item.archive_path for item in epub_io._translation_documents(book, archive, opf, base, False)],
                [item.archive_path for item in epub_io._translation_documents(book, archive, opf, base, True)])


# The current flow raises on a remote manifest item, which EPUB 3 allows, so these books hold none.
NO_REMOTE = b'<item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>'


def test_the_documents_found_agree_with_the_current_flow(tmp_path):
    path = tmp_path / "book.epub"
    path.write_bytes(epub_bytes(replace=opf_with(NO_REMOTE, b"")))
    without_navigation, everything = legacy_documents(path)
    with opened(path.read_bytes()) as archive:
        documents = archive.package.documents
        assert [item.archive_path for item in documents if not item.is_navigation] == without_navigation
        assert sorted(item.archive_path for item in documents) == sorted(everything)


def test_the_current_flow_agrees_on_a_text_html_item(tmp_path):
    path = tmp_path / "book.epub"
    path.write_bytes(epub_bytes(replace={"OEBPS/content.opf": OPF.replace(NO_REMOTE, b"").replace(b'media-type="text/css"', b'media-type="text/html"')}))
    with opened(path.read_bytes()) as archive:
        assert [item.archive_path for item in archive.package.documents if not item.is_navigation] == legacy_documents(path)[0]


# Links

@pytest.mark.parametrize("reference,base,expected", [
    ("a.xhtml", "", "a.xhtml"), ("a.xhtml", "text", "text/a.xhtml"), ("../a.xhtml#x", "text", "a.xhtml"), ("#x", "text", None), ("", "text", None),
    ("https://x.org/a", "text", None), ("mailto:a@b", "", None), ("tel:1", "", None), ("data:x", "", None),
], ids=range(9))
def test_internal_paths_resolve_as_validation_expects(reference, base, expected):
    assert resolve_internal_path(reference, base) == expected


# Privacy and boundaries

def test_errors_and_representations_hold_no_names_or_text():
    secret = "OEBPS/Call me Ishmael.css"
    with pytest.raises(EpubArchiveError) as error:
        opened(epub_bytes(replace=opf_with(b'href="style.css"', b'href="Call me Ishmael.css"')))
    assert "Ishmael" not in str(error.value)
    with opened(epub_bytes(add=[(secret, b"x", ZIP_STORED)])) as archive:
        for value in (archive, archive.package, archive.package.items[0]):
            assert "Ishmael" not in repr(value) and "The Title" not in repr(value) and "style.css" not in repr(value)


def test_the_module_imports_no_translation_html_or_interface():
    source = (Path(__file__).parents[1] / "src" / "ayvu" / "epub_archive.py").read_text()
    assert not re.search(r"^from \.|^import ayvu|^from ayvu", source, re.MULTILINE)
    assert "ebooklib" not in source and "bs4" not in source and "print(" not in source


def test_a_remote_manifest_item_stops_only_the_current_flow(tmp_path):
    path = tmp_path / "book.epub"
    path.write_bytes(epub_bytes())
    with pytest.raises(KeyError):
        epub.read_epub(str(path))
    with opened(path.read_bytes()) as archive:
        assert archive.package.documents


@pytest.mark.parametrize("data", [
    epub_bytes(drop=("mimetype",)),
    zip_bytes([*members()[1:], members()[0]]),
    zip_bytes([("mimetype", b"application/epub+zip", ZIP_DEFLATED), *members()[1:]]),
    epub_bytes(replace={"mimetype": b"text/plain"}),
    epub_bytes(replace={"mimetype": b"application/epub+zipX"}),
    zip_bytes([("first.txt", b"x" * 20, ZIP_STORED), *members()]),
], ids=["missing", "not-first", "compressed", "other-content", "longer-content", "stored-member-first"])
def test_a_defective_mimetype_is_accepted_and_a_copy_corrects_it(data):
    with opened(data) as archive:
        assert not archive.mimetype_is_valid
        before = [name for name in archive.names() if name != "mimetype"]
        output = io.BytesIO()
        archive.copy_to(output, {})
    written = output.getvalue()
    assert written[:4] == b"PK\x03\x04" and written[30:58] == b"mimetypeapplication/epub+zip"
    with opened(written) as copy:
        assert copy.mimetype_is_valid and copy.names() == ("mimetype", *before)
        assert all(copy.read(name, 1024 ** 2) == ZipFile(io.BytesIO(data)).read(name) for name in before)


def test_a_valid_mimetype_is_copied_as_it_is():
    data = epub_bytes()
    with opened(data) as archive:
        assert archive.mimetype_is_valid
        output = io.BytesIO()
        archive.copy_to(output, {})
    source, written = ZipFile(io.BytesIO(data)).getinfo("mimetype"), ZipFile(io.BytesIO(output.getvalue())).getinfo("mimetype")
    assert (written.date_time, written.external_attr, written.create_system) == (source.date_time, source.external_attr, source.create_system)
