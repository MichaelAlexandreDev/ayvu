import ast

import pytest

from ayvu.content.model import ContentDocument, LocaleState, SourceLocale
from ayvu.formats.contracts import UnitTranslation
from ayvu.formats.epub.html_structure import HtmlStructureError, reconstruct_html

from tests.formats.epub.html_structure_support import FIXTURES, extract
from tests.formats.epub.html_structure_support import serialized
from tests.support.paths import PROJECT_ROOT


def test_a_transient_document_has_no_identity_and_cannot_be_serialized():
    import copyreg
    import pickle

    from ayvu.formats.epub.html_structure import TransientHtmlDocument, TransientHtmlRun

    document = TransientHtmlDocument(FIXTURES["nested"])
    run = document.runs[0]
    for value in (document, run):
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            copyreg.dispatch_table.get(type(value), type(value).__reduce_ex__)(value, 2)
        assert not any(name in dir(value) for name in ("unit_id", "locator", "revision_id", "document_id"))
    assert "Hello" not in repr(TransientHtmlDocument("<p>Hello</p>")) and repr(run) == "TransientHtmlRun()" and isinstance(run, TransientHtmlRun)


@pytest.mark.parametrize("name", FIXTURES)
def test_a_transient_document_restores_unchanged_text_exactly_as_parsed(name):
    from ayvu.formats.epub.html_structure import TransientHtmlDocument, has_translatable_text

    document = TransientHtmlDocument(FIXTURES[name])
    for run in document.runs:
        if has_translatable_text(run.template):
            document.restore(run, run.template)
    assert document.encode() == serialized(FIXTURES[name])


def test_a_fragment_the_parser_cannot_read_shows_no_internal_marker(monkeypatch):
    from ayvu.formats.epub import html_structure as html_structure
    from ayvu.formats.epub.html_structure import NavigableString, TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>a <b>b</b></p></body></html>")
    # This is what the parser helper returns when no root element comes back.
    monkeypatch.setattr(html_structure, "parse_fragment_nodes", lambda fragment: [NavigableString(fragment)])
    document.restore(document.runs[0], "x __AYVU_TAG_0__y__AYVU_TAG_1__")
    output = document.encode().decode()
    assert "__ayvu_slot__" not in output and "x &lt;b&gt;y&lt;/b&gt;" in output


def test_a_token_a_transient_run_does_not_have_stays_as_text():
    from ayvu.formats.epub.html_structure import TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>x</p></body></html>")
    document.restore(document.runs[0], "x __AYVU_TAG_7__")
    assert b"<p>x __AYVU_TAG_7__</p>" in document.encode()


def test_a_transient_run_tolerates_a_closing_part_with_nothing_open():
    from ayvu.formats.epub.html_structure import TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>a <b>b</b></p></body></html>")
    document.restore(document.runs[0], "a __AYVU_TAG_1__b")
    assert b"<p>a b</p>" in document.encode()


def test_a_transient_run_closes_what_was_left_open():
    from ayvu.formats.epub.html_structure import TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>a <b>b</b> c</p></body></html>")
    document.restore(document.runs[0], "a __AYVU_TAG_0__b c")
    assert b"<p>a <b>b c</b></p>" in document.encode()


def test_errors_and_representations_hold_no_text():
    document = extract("<html><body><p>Call me Ishmael.</p></body></html>")
    assert "Ishmael" not in repr(document) and "Ishmael" not in repr(document.units[0])
    with pytest.raises(HtmlStructureError) as error:
        reconstruct_html("<html><body><p>Call me Ishmael.</p></body></html>", document, [UnitTranslation(document.units[0].unit_id, "Ishmael __AYVU_TAG_0__")])
    assert "Ishmael" not in str(error.value)


def test_the_result_is_a_content_document_with_the_given_identity():
    locale = SourceLocale(LocaleState.EXPLICIT, "en")
    document = extract(FIXTURES["plain"], source_locale=locale, source_fingerprint="sha256:" + "a" * 64)
    assert type(document) is ContentDocument and document.source_locale == locale and document.source_fingerprint == "sha256:" + "a" * 64


# Boundaries

def test_the_module_imports_no_provider_cache_glossary_or_interface():
    tree = ast.parse((PROJECT_ROOT / "src" / "ayvu" / "formats" / "epub" / "html_structure.py").read_text())
    forbidden = {"cache", "glossary", "translator", "translation_memory", "translation_pipeline", "translation_chunks", "provider", "provider_service",
                 "epub_io", "html_translate", "cli", "domain", "ebooklib", "typer", "rich", "requests", "sqlite3", "PySide6"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, node.module
        elif isinstance(node, ast.Import):
            assert not {alias.name for alias in node.names} & forbidden


def test_translated_text_is_never_handed_to_a_parser():
    source = (PROJECT_ROOT / "src" / "ayvu" / "formats" / "epub" / "html_structure.py").read_text()
    start = source.index("def _plan(")
    assert "BeautifulSoup(" not in source[start:].replace("BeautifulSoup(html, _PARSER)", "")
    assert "parse_fragment_nodes" not in source[start:] and "escape(" not in source[start:]
