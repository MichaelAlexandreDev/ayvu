

from ayvu.v1.html_translate import apply_reviewed_html

from tests.v1.html_translate_support import _resolver


def test_apply_reviewed_html_replaces_segment_text_by_index():
    html = "<html><body><p>Hello reader.</p><p>Second one.</p></body></html>"
    seen: list[tuple[int, str, str]] = []

    output, stats = apply_reviewed_html(html, _resolver({1: "Ola leitor."}, seen))

    result = output.decode("utf-8")
    assert "Ola leitor." in result
    assert "Second one." in result  # left untouched (resolver returned None)
    assert stats.applied == 1
    assert seen == [(1, "text", "Hello reader."), (2, "text", "Second one.")]


def test_apply_reviewed_html_flattens_inline_tags_in_replaced_segment():
    html = '<html><body><p>Hello <a href="ch.xhtml"><em>reader</em></a>.</p></body></html>'

    output, stats = apply_reviewed_html(html, _resolver({1: "Ola leitor."}))

    result = output.decode("utf-8")
    assert "Ola leitor." in result
    assert "<a" not in result
    assert "<em>" not in result
    assert stats.applied == 1


def test_apply_reviewed_html_escapes_special_characters():
    html = "<html><body><p>Tom and Jerry</p></body></html>"

    output, _stats = apply_reviewed_html(html, _resolver({1: "Tom & Jerry <3"}))

    result = output.decode("utf-8")
    assert "Tom &amp; Jerry &lt;3" in result


def test_apply_reviewed_html_applies_image_alt_after_text_runs():
    html = (
        '<html><body><p>Hello reader.</p>'
        '<p><img src="cover.png" alt="A red house" /></p></body></html>'
    )
    seen: list[tuple[int, str, str]] = []

    output, stats = apply_reviewed_html(html, _resolver({2: "Uma casa vermelha"}, seen))

    result = output.decode("utf-8")
    assert 'alt="Uma casa vermelha"' in result
    assert stats.applied == 1
    assert seen == [(1, "text", "Hello reader."), (2, "alt", "A red house")]
