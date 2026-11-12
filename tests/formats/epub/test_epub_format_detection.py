from zipfile import ZIP_DEFLATED, ZIP_STORED

import pytest

from ayvu.formats.epub.adapter import epub_format_adapter
from ayvu.formats.contracts import DetectionOutcome, FidelityAspect, FidelityLevel, FormatOperation, OperationSupport
from tests.support.epub_fakes import OPF, epub_bytes, members, zip_bytes
from ayvu.kernel import JobFailureFamily

from tests.formats.epub.epub_format_support import ALL_SCOPES, extract
from tests.formats.epub.epub_format_support import service, source_of


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
