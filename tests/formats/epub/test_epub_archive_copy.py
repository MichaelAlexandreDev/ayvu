import io
import re
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from ebooklib import epub

from ayvu.formats.epub.archive import EpubArchive, EpubArchiveError, resolve_internal_path
from tests.support.epub_fakes import OPF, epub_bytes, members, opf_with, zip_bytes

from tests.formats.epub.epub_archive_support import NO_REMOTE, copied, legacy_documents, opened
from tests.support.paths import PROJECT_ROOT


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
    source = (PROJECT_ROOT / "src" / "ayvu" / "formats" / "epub" / "archive.py").read_text()
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
