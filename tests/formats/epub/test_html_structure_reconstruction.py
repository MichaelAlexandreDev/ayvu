
import pytest
from bs4 import BeautifulSoup

from ayvu.formats.contracts import UnitTranslation
from ayvu.formats.epub.html_structure import HtmlStructureError, html_unit_template, reconstruct_html

from tests.formats.epub.html_structure_support import FIXTURES, NESTED, extract
from tests.formats.epub.html_structure_support import reconstruct, serialized, template, tokens_of


def test_translated_text_lands_inside_the_original_markup():
    output = reconstruct(["A __AYVU_TAG_0__B __AYVU_TAG_1__C__AYVU_TAG_2____AYVU_TAG_3__ D", "S __AYVU_TAG_0__X__AYVU_TAG_1__"]).decode()
    assert '<p>A <b id="k">B <i>C</i></b> D</p><p>S <b>X</b></p>' in output


def test_markup_can_move_while_it_still_nests():
    output = reconstruct(["__AYVU_TAG_0__B __AYVU_TAG_1__C__AYVU_TAG_2____AYVU_TAG_3__ primeiro", template(1)]).decode()
    assert '<p><b id="k">B <i>C</i></b> primeiro</p>' in output


@pytest.mark.parametrize("text,code", [
    ("no tokens", "missing_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2__", "missing_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3____AYVU_TAG_3__", "duplicate_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3____AYVU_TAG_4__", "invented_token"),
    ("__AYVU_TAG_99__", "invented_token"),
    ("__AYVU_TAG_00____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__", "malformed_token"),
    (f"__AYVU_TAG_{'9' * 5000}__", "malformed_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__ AYVU TAG 4", "malformed_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__ __AYVU_TAG_x__", "malformed_token"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__ __AYVU_PROTECTED_0__", "malformed_token"),
    ("__AYVU_TAG_3____AYVU_TAG_2____AYVU_TAG_1____AYVU_TAG_0__", "invalid_token_order"),
    ("__AYVU_TAG_1____AYVU_TAG_0____AYVU_TAG_2____AYVU_TAG_3__", "invalid_token_order"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_3____AYVU_TAG_2__", "invalid_token_order"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__\x0b", "invalid_text"),
    ("__AYVU_TAG_0____AYVU_TAG_1____AYVU_TAG_2____AYVU_TAG_3__￾", "invalid_text"),
], ids=range(15))
def test_corrupted_tokens_fail_with_their_own_code(text, code):
    with pytest.raises(HtmlStructureError, match=f"^{code}$"):
        reconstruct([text, template(1)])


def test_a_later_bad_unit_stops_the_whole_document():
    with pytest.raises(HtmlStructureError, match="^missing_token$"):
        reconstruct([template(0), "the token is gone"])


def test_a_token_from_another_unit_restores_only_this_units_markup():
    # The second unit has two parts, so the first unit's third and fourth do not exist here.
    with pytest.raises(HtmlStructureError, match="^invented_token$"):
        reconstruct([template(0), "__AYVU_TAG_2__x__AYVU_TAG_3__"])
    # Numbers that exist here mean this unit's own parts, whatever they meant elsewhere.
    output = reconstruct([template(0), "__AYVU_TAG_0__taken__AYVU_TAG_1__"]).decode()
    assert '<b>taken</b>' in output and output.count('id="k"') == 1


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>", '<a href="javascript:x">y</a>', "<b onclick='x'>y</b>", "&lt;b&gt;", "&amp;", "<!-- c -->", "<![CDATA[x]]>", "]]>", '" onload="x',
    "</p><p>", "<?pi x?>", "&#x3C;script&#x3E;",
], ids=range(12))
def test_translated_text_never_becomes_markup(hostile):
    html = "<html><body><p>x</p></body></html>"
    output = reconstruct([hostile], html=html)
    parsed = BeautifulSoup(output, "lxml-xml")
    paragraph = parsed.find("p")
    assert paragraph.get_text() == hostile and [child.name for child in paragraph.children] == [None]
    assert len(parsed.find_all("p")) == 1 and paragraph.attrs == {}


def test_hostile_alt_text_stays_inside_the_attribute():
    html = '<html><body><img src="i" alt="x"/></body></html>'
    document = extract(html, include_alt_text=True)
    hostile = '" onload="x()" <script>'
    image = BeautifulSoup(reconstruct_html(html, document, [UnitTranslation(document.units[0].unit_id, hostile)]), "lxml-xml").find("img")
    assert image["alt"] == hostile and set(image.attrs) == {"src", "alt"}


def test_a_literal_token_comes_back_unchanged():
    html = FIXTURES["literal_token"]
    document = extract(html)
    unit = document.units[0]
    assert html_unit_template(unit) == "__AYVU_TAG_0__ x __AYVU_TAG_1__y__AYVU_TAG_2__ __AYVU_TAG_3__"
    assert [part.kind for part in unit.content.protected_parts] == ["placeholder", "markup", "markup", "placeholder"]
    translated = html_unit_template(unit).replace(" x ", " X ")
    assert b"<p>__AYVU_TAG_0__ X <b>y</b> __AYVU_TAG_1__</p>" in reconstruct_html(html, document, [UnitTranslation(unit.unit_id, translated)])


def test_a_literal_token_in_alt_text_comes_back_unchanged():
    html = '<html><body><img alt="__AYVU_TAG_5__ world"/></body></html>'
    document = extract(html, include_alt_text=True)
    unit = document.units[0]
    assert html_unit_template(unit) == "__AYVU_TAG_0__ world"
    output = reconstruct_html(html, document, [UnitTranslation(unit.unit_id, "__AYVU_TAG_0__ mundo")])
    assert BeautifulSoup(output, "lxml-xml").find("img")["alt"] == "__AYVU_TAG_5__ mundo"


def test_source_text_that_only_resembles_a_marker_is_allowed_back():
    html = "<html><body><p>AYVU TAG 1 world</p></body></html>"
    assert b"AYVU TAG 1 mundo" in reconstruct(["AYVU TAG 1 mundo"], html=html)


def test_namespaces_and_comments_survive_a_translation():
    html = FIXTURES["namespaced"]
    document = extract(html)
    output = reconstruct_html(html, document, [UnitTranslation(unit.unit_id, html_unit_template(unit).replace("one", "um").replace("two", "dois")) for unit in document.units])
    parsed = BeautifulSoup(output, "lxml-xml")
    span, link = parsed.find("span"), parsed.find("a")
    assert span["epub:type"] == "pagebreak" and link["xlink:href"] == "#n" and link["epub:type"] == "noteref" and span.string == "um"
    html = FIXTURES["comments"]
    document = extract(html)
    output = reconstruct_html(html, document, tokens_of(document)).decode()
    assert "<!-- in -->" in output and "<?pi data?>" in output and "<!-- split -->" in output


def test_a_stale_source_is_refused():
    document = extract("<html><body><p>one</p></body></html>")
    with pytest.raises(HtmlStructureError, match="^stale_revision$"):
        reconstruct_html("<html><body><p>changed</p></body></html>", document, tokens_of(document))
    with pytest.raises(HtmlStructureError, match="^stale_revision$"):
        reconstruct_html("<html><body><p>one</p><p>two</p></body></html>", document, tokens_of(document))


def test_a_result_for_a_unit_of_another_revision_is_unknown():
    document = extract("<html><body><p>one</p></body></html>")
    other = extract("<html><body><p>two</p></body></html>")
    with pytest.raises(HtmlStructureError, match="^unknown_unit$"):
        reconstruct_html("<html><body><p>one</p></body></html>", document, [*tokens_of(document), *tokens_of(other)])


def test_missing_and_duplicate_results_are_refused():
    document = extract(NESTED)
    good = tokens_of(document)
    with pytest.raises(HtmlStructureError, match="^missing_translation$"):
        reconstruct_html(NESTED, document, good[:1])
    with pytest.raises(HtmlStructureError, match="^duplicate_translation$"):
        reconstruct_html(NESTED, document, [*good, good[0]])
    with pytest.raises(HtmlStructureError, match="^invalid_translation$"):
        reconstruct_html(NESTED, document, ["text"])


def test_a_preview_keeps_source_text_for_units_with_no_result():
    document = extract(NESTED)
    first = document.units[0]
    output = reconstruct_html(NESTED, document, [UnitTranslation(first.unit_id, html_unit_template(first).replace("a ", "A "))], partial=True).decode()
    assert "<p>A <b" in output and "<p>second <b>x</b></p>" in output
    assert reconstruct_html(NESTED, document, [], partial=True) == serialized(NESTED)


def test_unknown_results_are_refused_even_for_a_preview():
    document = extract(NESTED)
    with pytest.raises(HtmlStructureError, match="^unknown_unit$"):
        reconstruct_html(NESTED, document, [UnitTranslation("stranger", "x")], partial=True)


def test_a_failure_leaves_the_input_alone_and_returns_nothing():
    source = NESTED.encode()
    document = extract(source)
    before = bytes(source)
    with pytest.raises(HtmlStructureError):
        reconstruct_html(source, document, [UnitTranslation(document.units[0].unit_id, html_unit_template(document.units[0])),
                                            UnitTranslation(document.units[1].unit_id, "bad")])
    assert source == before


def test_nothing_is_applied_before_every_unit_is_built(monkeypatch):
    from ayvu.formats.epub import html_structure as html_structure

    applied = []
    monkeypatch.setattr(html_structure, "replace_run", lambda run, nodes: applied.append(1))
    document = extract(NESTED)
    with pytest.raises(HtmlStructureError):
        reconstruct_html(NESTED, document, [UnitTranslation(document.units[0].unit_id, html_unit_template(document.units[0])),
                                            UnitTranslation(document.units[1].unit_id, "bad")])
    assert applied == []
