
import pytest

from ayvu.formats.contracts import ExtractionRequest, FindingSeverity
from ayvu.formats.local import LocalOutputGuard
from tests.support.epub_fakes import OPF, XHTML, epub_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv

from tests.formats.epub.epub_format_support import rebuild, service, source_of


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
