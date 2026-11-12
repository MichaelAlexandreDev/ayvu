
import pytest
from bs4 import BeautifulSoup


from tests.v1.html_translate_support import IdentityOr, NAMESPACED
from tests.v1.html_translate_support import run_markup


@pytest.mark.parametrize("markup,expected", [
    ('<span epub:type="pagebreak" id="p1">world</span> z', '<span epub:type="pagebreak" id="p1">mundo</span> z'),
    ('<a epub:type="noteref" href="#n">world</a>', '<a epub:type="noteref" href="#n">mundo</a>'),
    ('<a xlink:href="#n">world</a>', '<a xlink:href="#n">mundo</a>'),
    ('<code epub:type="x">world</code> world', '<code epub:type="x">world</code> mundo'),
    ('<b><i epub:type="y">world</i></b>', '<b><i epub:type="y">mundo</i></b>'),
    ('<span xml:lang="en">world</span>', '<span xml:lang="en">mundo</span>'),
    ('<br epub:type="z"/>world', '<br epub:type="z"/>mundo'),
], ids=range(7))
def test_inline_namespaced_attributes_survive_translation(tmp_path, markup, expected):
    output, stats, _ = run_markup(tmp_path, NAMESPACED.format(markup))
    assert expected in output and stats.errors == []


@pytest.mark.parametrize("markup,expected", [
    ("a <b>world<!-- secret -->x</b> y", "a <b>mundo<!-- secret -->x</b> y"),
    ("a <b>world<?pi secret?>x</b>", "a <b>mundo<?pi secret?>x</b>"),
    ("<i><b><!-- secret -->world</b></i>", "<i><b><!-- secret -->mundo</b></i>"),
], ids=range(3))
def test_a_comment_inside_an_inline_tag_stays_a_comment(tmp_path, markup, expected):
    html = f"<html><body><p>{markup}</p></body></html>"
    segments = []
    output, _, translator = run_markup(tmp_path, html, on_segment_translated=segments.append)
    assert expected in output
    assert not any("secret" in text for text in translator.sent)
    assert "secret" not in segments[0].original and "secret" not in segments[0].translated


@pytest.mark.parametrize("markup,expected", [
    ("__AYVU_TAG_0__ world <b>x</b>", "__AYVU_TAG_0__ mundo <b>x</b>"),
    ("<b>x</b> __AYVU_TAG_0__ world", "<b>x</b> __AYVU_TAG_0__ mundo"),
    ("__AYVU_TAG_7__ world", "__AYVU_TAG_7__ mundo"),
    ("<b>__AYVU_TAG_0__</b> world", "<b>__AYVU_TAG_0__</b> mundo"),
    (" \n __AYVU_TAG_1__ world", " \n __AYVU_TAG_1__ mundo"),
], ids=range(5))
def test_book_text_that_looks_like_a_token_comes_back_unchanged(tmp_path, markup, expected):
    output, stats, _ = run_markup(tmp_path, f"<html><body><p>{markup}</p></body></html>")
    assert expected in output and stats.errors == []


def test_a_run_of_only_a_token_like_literal_is_skipped(tmp_path):
    output, stats, translator = run_markup(tmp_path, "<html><body><p>__AYVU_TAG_0__</p></body></html>")
    assert translator.sent == [] and stats.skipped == 1 and "__AYVU_TAG_0__" in output


def test_review_text_of_a_token_like_literal_is_the_literal(tmp_path):
    segments = []
    run_markup(tmp_path, "<html><body><p>__AYVU_TAG_0__ world <b>x</b></p></body></html>", on_segment_translated=segments.append)
    assert segments[0].original == "__AYVU_TAG_0__ world x" and segments[0].translated == "__AYVU_TAG_0__ mundo x"


def test_ordinary_text_keeps_its_cache_key(tmp_path):
    html = '<html><body><p>a <b id="k">world</b> <!-- out --><code>x</code></p></body></html>'
    from ayvu.formats.epub.html_structure import TransientHtmlDocument

    assert [run.template for run in TransientHtmlDocument(html).runs] == ["a __AYVU_TAG_0__world__AYVU_TAG_1__ ", "__AYVU_TAG_0__"]


@pytest.mark.parametrize("hostile", ["<script>x()</script>", "&amp;", "]]>", "<b>"], ids=range(4))
def test_translated_text_never_becomes_markup_in_the_current_flow(tmp_path, hostile):
    output, _, _ = run_markup(tmp_path, "<html><body><p>a <i>world</i></p></body></html>", IdentityOr(("world", hostile)))
    parsed = BeautifulSoup(output, "lxml-xml")
    assert parsed.find("script") is None and parsed.find("i").get_text() == hostile and parsed.find("i").find_all(True) == []


def test_an_invalid_character_from_a_provider_no_longer_damages_original_markup(tmp_path):
    html = "<html><body><p>world<code>a &amp; b</code></p></body></html>"
    output, _, _ = run_markup(tmp_path, html, IdentityOr(("world", "wo\x0brld")))
    assert "<code>a &amp; b</code>" in output


@pytest.mark.parametrize("markup", ["a <b>world</b>", '<a href="u">world</a> x<br/>y', "<span/>world", "<code>k</code> world"], ids=range(4))
def test_translating_again_from_the_cache_gives_the_same_output(tmp_path, markup):
    html = f"<html><body><p>{markup}</p></body></html>"
    first, _, _ = run_markup(tmp_path, html)
    second, _, _ = run_markup(tmp_path, html)
    assert first == second and "mundo" in first
