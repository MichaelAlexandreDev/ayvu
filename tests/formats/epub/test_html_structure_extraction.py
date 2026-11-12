
import pytest

from ayvu.content.model import AdapterIdentity, LocaleState, SourceLocale
from ayvu.formats.epub.html_structure import (
    HTML_ADAPTER, HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION, HtmlStructureError, extract_html_document, extract_visible_text, html_unit_template,
    reconstruct_html,
)

from tests.formats.epub.html_structure_support import FIXTURES, PROJECT, extract, generated
from tests.formats.epub.html_structure_support import serialized, tokens_of


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("alt", [False, True], ids=["no-alt", "alt"])
def test_unchanged_text_gives_exactly_what_parsing_gives(name, alt):
    html = FIXTURES[name]
    document = extract(html, include_alt_text=alt)
    assert reconstruct_html(html, document, tokens_of(document)) == serialized(html)


@pytest.mark.parametrize("seed", range(40))
def test_generated_documents_round_trip(seed):
    html = generated(seed)
    document = extract(html, include_alt_text=True)
    assert reconstruct_html(html, document, tokens_of(document)) == serialized(html)


def test_the_parser_reads_cdata_as_text_and_reconstruction_follows_it():
    html = "<html><body><p><![CDATA[a <b> c]]></p></body></html>"
    document = extract(html)
    assert reconstruct_html(html, document, tokens_of(document)) == serialized(html)
    assert b"a &lt;b&gt; c" in serialized(html)


@pytest.mark.parametrize("html,sources", [
    ("<html><body><p>one</p><p>two</p></body></html>", ["one", "two"]),
    ("<html><body>a<p>b</p>c</body></html>", ["a", "b", "c"]),
    ("<html><body><p>a<!-- c -->b</p></body></html>", ["a", "b"]),
    ("<html><body><p>a <b>b</b> c</p></body></html>", ["a <b>b</b> c"]),
    ("<html><body><p>a <code>b</code></p></body></html>", ["a <code>b</code>"]),
    ("<html><body><p> keep </p></body></html>", [" keep "]),
    ("<html><body><a><div>x</div></a></body></html>", ["x"]),
], ids=range(7))
def test_units_and_their_order(html, sources):
    assert [unit.source_text for unit in extract(html).units] == sources


@pytest.mark.parametrize("tag", ["script", "style", "code", "pre", "kbd", "samp", "svg", "math"])
def test_every_ignored_tag_is_never_translatable(tag):
    document = extract(f"<html><body><{tag}>secret</{tag}><p>visible</p></body></html>")
    assert [unit.source_text for unit in document.units] == ["visible"]


@pytest.mark.parametrize("tag", ["code", "kbd", "samp"])
def test_an_ignored_inline_tag_is_protected_whole(tag):
    html = f"<html><body><p>a <{tag} class='c'>secret <b>x</b></{tag}></p></body></html>"
    unit = extract(html).units[0]
    assert html_unit_template(unit) == "a __AYVU_TAG_0__" and unit.content.protected_parts[0].kind == "markup"
    assert "secret" in unit.source_text and "secret" not in html_unit_template(unit)


@pytest.mark.parametrize("html", [
    "<html><body><!-- secret --></body></html>", "<?xml version='1.0'?><html/>", "<!DOCTYPE html><html/>",
    "<?pi secret?><html/>", "<html><body><p>  </p>\n</body></html>", "<html><body><b> </b></body></html>",
    "<html><body><p><!-- secret --></p></body></html>", "<html><body><p><br/></p></body></html>", "<html><body><p><code>x</code></p></body></html>",
], ids=range(9))
def test_nothing_visible_gives_no_unit(html):
    assert extract(html).units == ()


def test_a_comment_inside_an_inline_tag_is_protected_not_text():
    unit = extract("<html><body><p>a <b>x<!-- secret -->y</b></p></body></html>").units[0]
    assert html_unit_template(unit) == "a __AYVU_TAG_0__x__AYVU_TAG_1__y__AYVU_TAG_2__"
    assert "<!-- secret -->" in unit.source_text


def test_outer_whitespace_stays_in_the_unit():
    assert extract("<html><body><p>  pad\t</p></body></html>").units[0].source_text == "  pad\t"


def test_alt_text_is_optional_comes_last_and_ignores_blanks():
    html = '<html><body><img alt="first"/><p>text</p><img alt="  "/><img/><img alt="second"/></body></html>'
    assert [unit.source_text for unit in extract(html).units] == ["text"]
    units = extract(html, include_alt_text=True).units
    assert [(unit.source_text, unit.content.kind.name) for unit in units] == [
        ("text", "paragraph"), ("first", "alt_text"), ("second", "alt_text")]


def test_extraction_does_not_change_its_input():
    source = bytearray(FIXTURES["nested"].encode())
    copy_of_source = bytes(source)
    extract(bytes(source))
    assert bytes(source) == copy_of_source


def test_visible_text_is_unchanged():
    assert extract_visible_text("<html><body><p>a <b>b</b><code>c</code></p><!-- d --><script>e</script></body></html>") == ["a ", "b"]


# Locators

def test_locators_are_versioned_and_hold_only_numbers():
    for unit in extract(FIXTURES["voids"], include_alt_text=True).units:
        locator = unit.content.locator
        assert (locator.schema, locator.schema_version) == (HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION)
        assert locator.value[:2] in ("r:", "a:") and not set(locator.value[2:]) - set("0123456789.+")


@pytest.mark.parametrize("name", FIXTURES)
def test_locators_are_deterministic_and_unique(name):
    first = extract(FIXTURES[name], include_alt_text=True)
    assert first == extract(FIXTURES[name], include_alt_text=True)
    values = [unit.content.locator.value for unit in first.units]
    assert len(values) == len(set(values))


def test_a_locator_states_a_position_in_the_tree_and_not_an_emission_order():
    html = '<html><body><p>a</p><pre>k</pre><p>b <i>c</i></p><img alt="d"/></body></html>'
    assert [unit.content.locator.value for unit in extract(html, include_alt_text=True).units] == ["r:0.0.0.0+1", "r:0.0.2.0+2", "a:0.0.3"]


def test_equal_text_at_two_places_has_two_identities():
    units = extract(FIXTURES["equal_siblings"]).units
    assert len({unit.unit_id for unit in units}) == 3 and len({unit.content.locator.value for unit in units}) == 3


def test_a_locator_does_not_depend_on_the_text():
    first = extract("<html><body><p>one</p><p>two</p></body></html>").units
    second = extract("<html><body><p>uno</p><p>dos</p></body></html>").units
    assert [unit.content.locator for unit in first] == [unit.content.locator for unit in second]


def test_changing_the_source_changes_the_revision():
    assert extract("<html><body><p>one</p></body></html>").revision_id != extract("<html><body><p>two</p></body></html>").revision_id


def test_rewriting_without_changing_content_keeps_the_identity():
    assert extract("<html><body><p>a &amp; b</p></body></html>").revision_id == extract("<html><body><p>a &#38; b</p></body></html>").revision_id


def test_the_segmenter_version_must_match():
    with pytest.raises(HtmlStructureError, match="^incompatible_segmenter$"):
        extract(FIXTURES["plain"], adapter=AdapterIdentity("epub", 1, 99))
    document = extract(FIXTURES["plain"], adapter=AdapterIdentity("epub", 1, 1))
    assert document.adapter.namespace == "epub"


def test_a_document_from_another_segmenter_version_is_refused():
    from ayvu.content.model import build_document

    other = build_document(PROJECT, "chapter.xhtml", AdapterIdentity("html_structure", 1, 2), SourceLocale(LocaleState.UNKNOWN),
                           [unit.content for unit in extract(FIXTURES["plain"]).units])
    with pytest.raises(HtmlStructureError, match="^incompatible_segmenter$"):
        reconstruct_html(FIXTURES["plain"], other, [])


def test_a_document_with_another_locator_is_refused():
    from ayvu.content.model import Locator, SemanticKind, UnitContent, build_document

    other = build_document(PROJECT, "chapter.xhtml", HTML_ADAPTER, SourceLocale(LocaleState.UNKNOWN),
                           [UnitContent(Locator("webvtt_cue", 1, "a"), "x", SemanticKind("paragraph"), (1,))])
    with pytest.raises(HtmlStructureError, match="^incompatible_locator$"):
        reconstruct_html(FIXTURES["plain"], other, [])
    newer = build_document(PROJECT, "chapter.xhtml", HTML_ADAPTER, SourceLocale(LocaleState.UNKNOWN),
                           [UnitContent(Locator(HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION + 1, "r:0+1"), "x", SemanticKind("paragraph"), (1,))])
    with pytest.raises(HtmlStructureError, match="^incompatible_locator$"):
        reconstruct_html(FIXTURES["plain"], newer, [])


@pytest.mark.parametrize("kind", [str, type(None)])
def test_a_value_that_is_not_a_document_is_refused(kind):
    with pytest.raises(HtmlStructureError, match="^invalid_document$"):
        reconstruct_html(FIXTURES["plain"], kind(), [])


@pytest.mark.parametrize("value", [None, 7, ["<p/>"]])
def test_html_must_be_text_or_bytes(value):
    with pytest.raises(HtmlStructureError, match="^invalid_html$"):
        extract(value)


@pytest.mark.parametrize("arguments", [(PROJECT, "/etc/passwd"), (PROJECT, "https://x.org/a"), ("not-a-uuid", "k")], ids=range(3))
def test_identity_is_validated(arguments):
    with pytest.raises(HtmlStructureError):
        extract_html_document(FIXTURES["plain"], *arguments)


def test_an_oversized_unit_is_reported_with_a_code():
    with pytest.raises(HtmlStructureError, match="^invalid_source_text$"):
        extract(f"<html><body><p>{'x' * (1024 * 1024 + 1)}</p></body></html>")
