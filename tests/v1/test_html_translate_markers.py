from collections import Counter

import pytest
from bs4 import BeautifulSoup

from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.knowledge.memory.fuzzy import TranslationMemory
from ayvu.v1.html_translate import _protected_terms_are_intact, translate_html, translate_text
from ayvu.kernel import LanguagePair, TranslationMemoryOptions

from tests.v1.html_translate_support import DamagingMarkerTranslator, FakeTranslator


@pytest.mark.parametrize("damage", ["spaces", "drop", "duplicate", "unknown", "reverse"])
def test_corrupted_markers_recover_terms_and_cache_only_safe_result(tmp_path, damage):
    original = "  Read CacheKey and CacheKey with https://example.com.  "
    expected = "  Leia CacheKey e CacheKey com https://example.com.  "
    translator = DamagingMarkerTranslator(damage)
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        first = translate_text(original, translator, cache, "en", "pt")
        call_count = len(translator.calls)
        second = translate_text(original, translator, cache, "en", "pt")
        assert first.text == second.text == expected
        assert second.from_cache
        assert len(translator.calls) == call_count
        assert all("AYVU" not in call for call in translator.calls[1:])


def test_marker_fallback_preserves_nested_markup_and_opaque_content(tmp_path):
    html = '<html><body><p>Read <a href="chapter.xhtml"><em>CacheKey</em></a> with <code>private_code()</code><br/>care.</p></body></html>'
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(html, DamagingMarkerTranslator(), cache, "en", "pt")
    soup = BeautifulSoup(output, "lxml-xml")
    assert soup.p.a["href"] == "chapter.xhtml"
    assert soup.p.a.em.string == "CacheKey"
    assert soup.p.code.string == "private_code()"
    assert soup.p.br is not None
    assert "Leia " in str(soup.p) and "cuidado." in str(soup.p)
    assert "AYVU" not in str(soup)
    assert stats.translated == 1 and not stats.errors


@pytest.mark.parametrize("cached", [
    "AYVU PROTECTED 0 CacheKey",
    "__AYVU_PROTECTED_0__ CacheKey",
    "Leia __AYVU_TAG_0__ CacheKey",
    "Leia com cuidado.",
    "Leia xxCacheKey com cuidado.",
    "Leia CacheKeyX com cuidado.",
    "Leia CacheKey CacheKey com cuidado.",
])
@pytest.mark.parametrize("mode", ["online", "cache_only", "dry_run"])
def test_poisoned_cache_is_retranslated_or_reported_missing(tmp_path, cached, mode):
    original = "Read CacheKey with care."
    key = CacheKey(text=original, language_pair=LanguagePair("en", "pt"))
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(key, cached)
        result = translate_text(original, translator, cache, "en", "pt", **({mode: True} if mode != "online" else {}))
        assert not result.from_cache
        if mode == "online":
            assert result.text == "Leia CacheKey com cuidado."
            assert cache.get(key) == result.text
        else:
            assert result.text == original
            assert not translator.calls
            assert result.missing == (mode == "cache_only")
            assert cache.get(key) == cached


@pytest.mark.parametrize("failure", ["error", "empty", "marker"])
def test_failed_marker_recovery_keeps_original_html_and_does_not_cache(tmp_path, failure):
    original = "Read CacheKey with care."
    html = f"<html><body><p>{original}</p></body></html>"
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(html, DamagingMarkerTranslator(fallback=failure), cache, "en", "pt")
        assert original in output.decode()
        assert stats.errors and stats.translated == 0
        assert cache.get(CacheKey(text=original, language_pair=LanguagePair("en", "pt"))) is None
        with pytest.raises((RuntimeError, ValueError)):
            translate_html(html, DamagingMarkerTranslator(fallback=failure), cache, "en", "pt", fail_fast=True)


def test_tiny_chunks_never_send_broken_markers_and_preserve_spaces(tmp_path):
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        result = translate_text("Read CacheKey with care.", translator, cache, "en", "pt", chunk_limit=5)
    assert result.text == "Leia CacheKey com cuidado."
    assert all("AYVU" not in call and len(call) <= 5 for call in translator.calls)


def test_fuzzy_memory_rejects_marker_leaks(tmp_path):
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(CacheKey(text="Read with care.", language_pair=LanguagePair("en", "pt")), "AYVU PROTECTED 0")
        memory = TranslationMemory(cache, TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7))
        result = translate_text("Read with care!", translator, cache, "en", "pt", memory=memory)
    assert result.text == "Leia com cuidado!"
    assert not result.from_memory and result.memory_suggestion is None


def test_fuzzy_memory_rejects_concatenated_protected_terms(tmp_path):
    original = "Read CacheKey with care."
    key = CacheKey(text=original, language_pair=LanguagePair("en", "pt"))
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(key, "Leia xxCacheKey com cuidado.")
        memory = TranslationMemory(
            cache,
            TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7),
        )
        result = translate_text(original, translator, cache, "en", "pt", memory=memory)
    assert result.text == "Leia CacheKey com cuidado."
    assert not result.from_cache and not result.from_memory
    assert result.memory_suggestion is None


def test_marker_recovery_stops_at_global_call_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.v1.html_translate.MAX_PROTECTED_RECOVERY_CALLS", 2)
    original = "Read CacheKey with care. Read CacheKey with care."
    key = CacheKey(text=original, language_pair=LanguagePair("en", "pt"))
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        with pytest.raises(ValueError, match="limite de chamadas"):
            translate_text(original, translator, cache, "en", "pt")
        assert cache.get(key) is None
    assert len(translator.calls) == 3  # Initial attempt plus two recovery calls.
    assert all("AYVU" not in call for call in translator.calls[1:])


def test_alt_text_marker_recovery(tmp_path):
    html = '<html><body><img src="cover.png" alt="Read CacheKey with care."/></body></html>'
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(html, DamagingMarkerTranslator(), cache, "en", "pt", translate_alt_text=True)
    soup = BeautifulSoup(output, "lxml-xml")
    assert soup.img["alt"] == "Leia CacheKey com cuidado."
    assert soup.img["src"] == "cover.png"
    assert stats.alt_translated == 1


@pytest.mark.parametrize("cached", [
    "Leia CacheKey.",
    "Leia __AYVU_TAG_1__CacheKey__AYVU_TAG_0__.",
    "Leia __AYVU_TAG_0__CacheKey__AYVU_TAG_1____AYVU_TAG_1__.",
])
def test_cache_with_lost_or_reordered_tags_is_repaired(tmp_path, cached):
    template = "Read __AYVU_TAG_0__CacheKey__AYVU_TAG_1__."
    key = CacheKey(text=template, language_pair=LanguagePair("en", "pt"))
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(key, cached)
        output, stats = translate_html(
            "<html><body><p>Read <em>CacheKey</em>.</p></body></html>",
            DamagingMarkerTranslator(), cache, "en", "pt",
        )
        assert cache.get(key) == "Leia __AYVU_TAG_0__CacheKey__AYVU_TAG_1__."
    assert "<p>Leia <em>CacheKey</em>.</p>" in output.decode()
    assert stats.from_cache == 0 and not stats.errors


def test_literal_marker_in_source_is_restored_without_recursive_substitution(tmp_path):
    original = "Read __AYVU_PROTECTED_1__ with CacheKey."
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        result = translate_text(original, FakeTranslator(), cache, "en", "pt")
    assert result.text == f"PT:{original}"


def test_cache_rejects_reordered_protected_and_tag_placeholders(tmp_path):
    original = "Read __AYVU_PROTECTED_5__ __AYVU_TAG_0__ CacheKey"
    key = CacheKey(text=original, language_pair=LanguagePair("en", "pt"))
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(key, "Leia __AYVU_TAG_0__ __AYVU_PROTECTED_5__ CacheKey")
        result = translate_text(original, translator, cache, "en", "pt")
        assert cache.get(key) == result.text
    assert result.text == "Leia __AYVU_PROTECTED_5__ __AYVU_TAG_0__ CacheKey"
    assert not result.from_cache


def test_protected_term_scanner_bounds_suffix_match_work():
    repeated_units = 100
    terms = Counter(
        {
            "A!!" * (length - 1) + "A!": repeated_units - length + 1
            for length in range(1, repeated_units + 1)
        }
    )
    assert not _protected_terms_are_intact("A!!" * repeated_units, terms)


def test_protected_term_scanner_counts_valid_suffix_matches():
    terms = Counter({"foo": 1, "bar-foo": 1})
    assert _protected_terms_are_intact("bar-foo", terms)


def test_marker_fallback_copies_whitespace_only_chunks_without_http(tmp_path):
    translator = DamagingMarkerTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        result = translate_text("Read      with\t\tCacheKey.", translator, cache, "en", "pt", chunk_limit=5)
    assert result.text == "Leia      com\t\tCacheKey."
    assert all(call.strip() for call in translator.calls)


@pytest.mark.parametrize("damage", [False, True])
def test_technical_terms_touching_inline_tags_never_reach_provider(tmp_path, damage):
    class AcronymTranslator(DamagingMarkerTranslator):
        def translate(self, text, source, target):
            if damage:
                return super().translate(text, source, target).replace("AI", "IA")
            self.calls.append(text)
            return text.replace("Read", "Leia").replace("with", "com").replace("AI", "IA")

    translator = AcronymTranslator()
    html = '<html><body><p>Read AI with <em>AI</em> and <a href="help.xhtml">CacheKey</a>.</p></body></html>'
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        output, stats = translate_html(html, translator, cache, "en", "pt", fail_fast=True)
    decoded = output.decode()
    assert "Leia AI com <em>AI</em>" in decoded
    assert '<a href="help.xhtml">CacheKey</a>' in decoded
    assert "IA" not in decoded and "AYVU" not in decoded
    assert all("AI" not in call and "CacheKey" not in call for call in translator.calls)
    assert not stats.errors
