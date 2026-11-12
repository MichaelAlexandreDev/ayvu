

from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.v1.html_translate import translate_html, translate_text
from ayvu.kernel import LanguagePair

from tests.v1.html_translate_support import FakeTranslator


def test_translate_text_protects_special_terms_before_translating(tmp_path):
    text = (
        "Use `uv run ayvu --help`, open https://example.com/docs, edit "
        "src/ayvu/html_translate.py, run git status, keep v1.2.0, call "
        "translate_text(), and pass CacheKey."
    )
    protected_terms = [
        "`uv run ayvu --help`",
        "https://example.com/docs",
        "src/ayvu/html_translate.py",
        "git status",
        "v1.2.0",
        "translate_text()",
        "CacheKey",
    ]

    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        result = translate_text(text, translator, cache, "en", "pt")

    translated_input = translator.calls[0]
    for term in protected_terms:
        assert term not in translated_input
        assert term in result.text
    assert "__AYVU_PROTECTED_" in translated_input
    assert "__AYVU_PROTECTED_" not in result.text


def test_translate_text_caches_restored_protected_terms(tmp_path):
    text = "Call translate_text() at https://example.com/docs."
    translator = FakeTranslator()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        first_result = translate_text(text, translator, cache, "en", "pt")
        second_result = translate_text(text, translator, cache, "en", "pt")

    assert first_result.text == "PT:Call translate_text() at https://example.com/docs."
    assert second_result.text == first_result.text
    assert second_result.from_cache
    assert translator.calls == ["Call __AYVU_PROTECTED_0__ at __AYVU_PROTECTED_1__."]


def test_translate_html_preserves_image_alt_by_default(tmp_path):
    html = '<html><body><p><img src="cover.png" alt="A red house" /></p></body></html>'
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(html, translator, cache, "en", "pt")

    result = output.decode("utf-8")
    assert 'alt="A red house"' in result
    assert "A red house" not in translator.calls
    assert stats.alt_translated == 0


def test_translate_html_translates_image_alt_when_enabled(tmp_path):
    html = '<html><body><p><img src="cover.png" alt="A red house" /></p></body></html>'
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(
            html, translator, cache, "en", "pt", translate_alt_text=True
        )

    result = output.decode("utf-8")
    # The alt text is translated while src and the image element are preserved.
    assert 'alt="PT:A red house"' in result
    assert 'src="cover.png"' in result
    assert "A red house" in translator.calls
    assert stats.alt_translated == 1
    assert stats.translated == 1


def test_translate_html_reports_review_segments_for_image_alt(tmp_path):
    html = '<html><body><p><img src="cover.png" alt="A red house" /></p></body></html>'
    segments = []
    translator = FakeTranslator()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_html(
            html,
            translator,
            cache,
            "en",
            "pt",
            translate_alt_text=True,
            on_segment_translated=segments.append,
        )

    assert len(segments) == 1
    assert segments[0].kind == "alt"
    assert segments[0].original == "A red house"
    assert segments[0].translated == "PT:A red house"


def test_translate_html_skips_empty_or_missing_image_alt(tmp_path):
    html = (
        '<html><body><p>'
        '<img src="decor.png" alt="" />'
        '<img src="logo.png" />'
        "</p></body></html>"
    )
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(
            html, translator, cache, "en", "pt", translate_alt_text=True
        )

    result = output.decode("utf-8")
    assert 'alt=""' in result
    assert translator.calls == []
    assert stats.alt_translated == 0


def test_translate_html_uses_cache_for_image_alt(tmp_path):
    html = '<html><body><p><img src="cover.png" alt="A red house" /></p></body></html>'
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(
            CacheKey(text="A red house", language_pair=LanguagePair(source="en", target="pt")),
            "Uma casa vermelha",
        )
        output, stats = translate_html(
            html, translator, cache, "en", "pt", translate_alt_text=True
        )

    result = output.decode("utf-8")
    assert 'alt="Uma casa vermelha"' in result
    assert translator.calls == []
    assert stats.alt_translated == 1
    assert stats.from_cache == 1
