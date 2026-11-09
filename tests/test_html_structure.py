import ast
import random
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from ayvu.content import AdapterIdentity, ContentDocument, LocaleState, SourceLocale
from ayvu.format_adapter import UnitTranslation
from ayvu.html_structure import (
    HTML_ADAPTER, HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION, HtmlStructureError, extract_html_document, extract_visible_text, html_unit_template,
    reconstruct_html,
)

PROJECT = "00000000-0000-4000-8000-000000000143"
XHTML = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:xlink="http://www.w3.org/1999/xlink">'
FIXTURES = {
    "plain": "<html><body><p>Hello world</p><p> spaced </p></body></html>",
    "nested": '<html><body><p>a <b>bold <i class="x">deep</i></b> <a href="u?a=1&amp;b=2" id="l">link</a> end</p></body></html>',
    "mixed": "<html><body>loose<p>inside</p>tail<div>more<p>x</p>after</div></body></html>",
    "ignored_inline": "<html><body><p>a <code>code()</code> b <kbd>K</kbd> <samp>s</samp></p></body></html>",
    "ignored_blocks": "<html><body><pre>keep</pre><script>x()</script><style>y{}</style><svg><text>t</text></svg><math><mi>m</mi></math><p>ok</p></body></html>",
    "namespaced": f'{XHTML}<body><section epub:type="chapter"><p><span epub:type="pagebreak" id="p1">one</span> <a xlink:href="#n" epub:type="noteref">two</a></p></section></body></html>',
    "comments": "<html><body><!-- out --><p>a<!-- split -->b</p><p>c <b>d<!-- in -->e<?pi data?></b></p></body></html>",
    "declarations": '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html><head><title>t</title></head><body><p>x</p></body></html>',
    "entities": "<html><body><p>a &amp; b &lt;c&gt;   café \U0001F600</p></body></html>",
    "voids": '<html><body><p>a<br/>b<span/><a id="z"/><wbr/></p><img src="i.png" alt="pic"/></body></html>',
    "malformed": "<html><body><p>a<b>b</p></body>",
    "whitespace": "<html><body><p>   </p>\n\n<p> x </p> <b> </b></body></html>",
    "inline_with_block": '<html><body><a href="u"><div>x</div></a></body></html>',
    "literal_token": "<html><body><p>__AYVU_TAG_0__ x <b>y</b> __AYVU_TAG_1__</p></body></html>",
    "equal_siblings": "<html><body><p>same</p><p>same</p><p>same</p></body></html>",
}


def extract(html, **options):
    return extract_html_document(html, PROJECT, "chapter.xhtml", **options)


def tokens_of(document):
    return [UnitTranslation(unit.unit_id, html_unit_template(unit)) for unit in document.units]


def serialized(html):
    return BeautifulSoup(html, "lxml-xml").encode(formatter="minimal")


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("alt", [False, True], ids=["no-alt", "alt"])
def test_unchanged_text_gives_exactly_what_parsing_gives(name, alt):
    html = FIXTURES[name]
    document = extract(html, include_alt_text=alt)
    assert reconstruct_html(html, document, tokens_of(document)) == serialized(html)


def generated(seed):
    rng = random.Random(seed)
    words = ["world", "a &amp; b", "&lt;x&gt;", " pad ", "café", "__AYVU_TAG_1__", " ", "line\nbreak"]
    attributes = ['', ' id="a"', ' epub:type="noteref"', ' xlink:href="#n"', ' xml:lang="pt"', ' title="a &amp; b"']

    def inline(depth=0):
        roll = rng.random()
        if roll < .5 or depth > 3:
            return rng.choice(words)
        if roll < .6:
            return rng.choice(["<br/>", "<!-- c -->", "<?pi x?>", "<code>k()</code>", "<span/>", " ", "\n"])
        tag = rng.choice(["b", "i", "a", "span", "em", "sub"])
        return f"<{tag}{rng.choice(attributes)}>{''.join(inline(depth + 1) for _ in range(rng.randint(1, 3)))}</{tag}>"

    def block(depth=0):
        roll = rng.random()
        if roll < .1:
            tag = rng.choice(["pre", "script", "svg"])
            return f"<{tag}>keep</{tag}>"
        if roll < .2:
            return '<img src="i" alt="an image"/>'
        if roll < .3 and depth < 2:
            return f"<div>{''.join(block(depth + 1) for _ in range(2))}</div>"
        return f"<p>{''.join(inline() for _ in range(rng.randint(1, 4)))}</p>"

    return f"{XHTML}<body>{''.join(block() for _ in range(rng.randint(1, 5)))}</body></html>"


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
    from ayvu.content import build_document

    other = build_document(PROJECT, "chapter.xhtml", AdapterIdentity("html_structure", 1, 2), SourceLocale(LocaleState.UNKNOWN),
                           [unit.content for unit in extract(FIXTURES["plain"]).units])
    with pytest.raises(HtmlStructureError, match="^incompatible_segmenter$"):
        reconstruct_html(FIXTURES["plain"], other, [])


def test_a_document_with_another_locator_is_refused():
    from ayvu.content import Locator, SemanticKind, UnitContent, build_document

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


# Reconstruction

NESTED = '<html><body><p>a <b id="k">b <i>c</i></b> d</p><p>second <b>x</b></p></body></html>'


def reconstruct(texts, html=NESTED, **options):
    document = extract(html)
    return reconstruct_html(html, document, [UnitTranslation(unit.unit_id, text) for unit, text in zip(document.units, texts)], **options)


def template(index=0, html=NESTED):
    return html_unit_template(extract(html).units[index])


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
    from ayvu import html_structure

    applied = []
    monkeypatch.setattr(html_structure, "replace_run", lambda run, nodes: applied.append(1))
    document = extract(NESTED)
    with pytest.raises(HtmlStructureError):
        reconstruct_html(NESTED, document, [UnitTranslation(document.units[0].unit_id, html_unit_template(document.units[0])),
                                            UnitTranslation(document.units[1].unit_id, "bad")])
    assert applied == []


def test_a_transient_document_has_no_identity_and_cannot_be_serialized():
    import copyreg
    import pickle

    from ayvu.html_structure import TransientHtmlDocument, TransientHtmlRun

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
    from ayvu.html_structure import TransientHtmlDocument, has_translatable_text

    document = TransientHtmlDocument(FIXTURES[name])
    for run in document.runs:
        if has_translatable_text(run.template):
            document.restore(run, run.template)
    assert document.encode() == serialized(FIXTURES[name])


def test_a_fragment_the_parser_cannot_read_shows_no_internal_marker(monkeypatch):
    from ayvu import html_structure
    from ayvu.html_structure import NavigableString, TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>a <b>b</b></p></body></html>")
    # This is what the parser helper returns when no root element comes back.
    monkeypatch.setattr(html_structure, "parse_fragment_nodes", lambda fragment: [NavigableString(fragment)])
    document.restore(document.runs[0], "x __AYVU_TAG_0__y__AYVU_TAG_1__")
    output = document.encode().decode()
    assert "__ayvu_slot__" not in output and "x &lt;b&gt;y&lt;/b&gt;" in output


def test_a_token_a_transient_run_does_not_have_stays_as_text():
    from ayvu.html_structure import TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>x</p></body></html>")
    document.restore(document.runs[0], "x __AYVU_TAG_7__")
    assert b"<p>x __AYVU_TAG_7__</p>" in document.encode()


def test_a_transient_run_tolerates_a_closing_part_with_nothing_open():
    from ayvu.html_structure import TransientHtmlDocument

    document = TransientHtmlDocument("<html><body><p>a <b>b</b></p></body></html>")
    document.restore(document.runs[0], "a __AYVU_TAG_1__b")
    assert b"<p>a b</p>" in document.encode()


def test_a_transient_run_closes_what_was_left_open():
    from ayvu.html_structure import TransientHtmlDocument

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
    tree = ast.parse((Path(__file__).parents[1] / "src" / "ayvu" / "html_structure.py").read_text())
    forbidden = {"cache", "glossary", "translator", "translation_memory", "translation_pipeline", "translation_chunks", "provider", "provider_service",
                 "epub_io", "html_translate", "cli", "domain", "ebooklib", "typer", "rich", "requests", "sqlite3", "PySide6"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, node.module
        elif isinstance(node, ast.Import):
            assert not {alias.name for alias in node.names} & forbidden


def test_translated_text_is_never_handed_to_a_parser():
    source = (Path(__file__).parents[1] / "src" / "ayvu" / "html_structure.py").read_text()
    start = source.index("def _plan(")
    assert "BeautifulSoup(" not in source[start:].replace("BeautifulSoup(html, _PARSER)", "")
    assert "parse_fragment_nodes" not in source[start:] and "escape(" not in source[start:]
