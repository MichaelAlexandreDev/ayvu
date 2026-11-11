import io
import re
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
import pytest
from ebooklib import epub
from ayvu.formats.epub import io as epub_io
from ayvu.formats.epub.archive import DEFAULT_LIMITS, EpubArchive, EpubArchiveError, EpubLimits, declares_entities, resolve_internal_path
from tests.support.epub_fakes import CONTAINER, OPF, epub_bytes, members, opf_with, zip_bytes

def opened(data, **options):
    return EpubArchive(io.BytesIO(data), **options)


def refused(data, code, **options):
    with pytest.raises(EpubArchiveError, match=f"^{code}$"):
        opened(data, **options)


def encryption(uri, algorithm):
    return (f'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
            f'<enc:EncryptionMethod Algorithm="{algorithm}"/><enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData></enc:EncryptedData></encryption>').encode()


# Copying

def copied(data, replacements, **options):
    output = io.BytesIO()
    with opened(data) as archive:
        archive.copy_to(output, replacements, **options)
    return output.getvalue()


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
