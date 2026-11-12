

from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.knowledge.glossary.rules import (
    GLOSSARY_RULE_FORBIDDEN,
    GLOSSARY_RULE_PRESERVE,
    GLOSSARY_RULE_TRANSLATE,
    Glossary,
    GlossaryEntry,
)
from ayvu.v1.html_translate import translate_html, translate_text
from ayvu.kernel import LanguagePair

from tests.v1.html_translate_support import FakeTranslator
from tests.v1.html_translate_support import _seed_memory


def test_translate_text_reuses_memory_for_similar_text(tmp_path):
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        memory = _seed_memory(cache)

        result = translate_text("I have a cat!", translator, cache, "en", "pt", memory=memory)

        assert result.from_memory is True
        assert result.text == "Eu tenho um gato."
        assert translator.calls == []
        # No auto-promotion: the fuzzy hit is not written under the new text.
        assert cache.get(CacheKey(text="I have a cat!", language_pair=LanguagePair("en", "pt"))) is None


def test_translate_text_suggests_without_reuse_in_middle_band(tmp_path):
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        memory = _seed_memory(cache, apply_threshold=0.97, suggest_threshold=0.6)

        result = translate_text("I have a dog.", translator, cache, "en", "pt", memory=memory)

    assert result.from_memory is False
    assert result.memory_suggestion is not None
    assert result.memory_suggestion.applied is False
    assert result.text == "PT:I have a dog."
    assert translator.calls == ["I have a dog."]


def test_translate_html_reuses_memory_for_similar_plain_block(tmp_path):
    html = "<html><body><p>I have a cat!</p></body></html>"
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        memory = _seed_memory(cache)

        output, stats = translate_html(html, translator, cache, "en", "pt", memory=memory)

    assert "Eu tenho um gato." in output.decode("utf-8")
    assert stats.from_memory == 1
    assert translator.calls == []


def test_translate_html_memory_skips_blocks_with_inline_tags(tmp_path):
    # Blocks carrying inline-tag placeholders must never reuse a fuzzy match,
    # so another segment's markup can never be injected.
    html = "<html><body><p>I have a <em>cat</em>!</p></body></html>"
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        memory = _seed_memory(cache, apply_threshold=0.5, suggest_threshold=0.4)

        _output, stats = translate_html(html, translator, cache, "en", "pt", memory=memory)

    assert stats.from_memory == 0
    assert len(translator.calls) == 1


def test_translate_text_cache_only_marks_missing_without_calling_translator(tmp_path):
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        result = translate_text(
            "Keep me",
            translator=translator,
            cache=cache,
            source="en",
            target="pt",
            cache_only=True,
        )

    assert result.missing
    assert not result.from_cache
    assert result.text == "Keep me"
    assert translator.calls == []


def test_translate_text_cache_only_uses_cache_when_available(tmp_path):
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(
            CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt")),
            "Mantenha-me",
        )
        result = translate_text(
            "Keep me",
            translator=translator,
            cache=cache,
            source="en",
            target="pt",
            cache_only=True,
        )

    assert result.from_cache
    assert not result.missing
    assert result.text == "Mantenha-me"
    assert translator.calls == []


def test_translate_html_cache_only_keeps_missing_text_and_reports_it(tmp_path):
    translator = FakeTranslator()
    html = "<html><body><p>Keep me</p><p>Hello reader.</p></body></html>"
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(
            CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt")),
            "Mantenha-me",
        )
        output, stats = translate_html(html, translator, cache, "en", "pt", cache_only=True)

    decoded = output.decode("utf-8")
    assert "Mantenha-me" in decoded
    assert "Hello reader." in decoded
    assert stats.from_cache == 1
    assert stats.missing == 1
    assert stats.translated == 0
    assert stats.missing_texts == ["Hello reader."]
    assert translator.calls == []


def test_translate_html_reports_review_segments_as_visible_text(tmp_path):
    html = '<html><body><p>Hello <a href="chapter.xhtml">reader</a>.</p></body></html>'
    segments = []
    translator = FakeTranslator()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_html(
            html,
            translator,
            cache,
            "en",
            "pt",
            on_segment_translated=segments.append,
        )

    assert len(segments) == 1
    assert segments[0].kind == "text"
    assert segments[0].original == "Hello reader."
    assert segments[0].translated == "PT:Hello reader."
    assert not segments[0].from_cache


def test_translate_html_reports_review_segments_from_cache(tmp_path):
    segments = []
    translator = FakeTranslator()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache.set(
            CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt")),
            "Mantenha-me",
        )
        translate_html(
            "<html><body><p>Keep me</p></body></html>",
            translator,
            cache,
            "en",
            "pt",
            on_segment_translated=segments.append,
        )

    assert len(segments) == 1
    assert segments[0].original == "Keep me"
    assert segments[0].translated == "Mantenha-me"
    assert segments[0].from_cache
    assert translator.calls == []


def test_translate_text_result_reports_cache_hit(tmp_path):
    translator = FakeTranslator()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache_key = CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt"))
        cache.set(cache_key, "Mantenha-me")

        result = translate_text(" Keep me ", translator, cache, "en", "pt")

    assert result.text == " Mantenha-me "
    assert result.from_cache
    assert translator.calls == []


def test_translate_text_applies_advanced_glossary_rules_to_cache_hits(tmp_path):
    translator = FakeTranslator()
    glossary = Glossary(
        [
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE),
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo"),
        ]
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache_key = CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt"))
        cache.set(cache_key, "observer uses the Game Loop")

        result = translate_text("Keep me", translator, cache, "en", "pt", glossary=glossary)

    assert result.text == "Observer uses the loop de jogo"
    assert result.from_cache
    assert translator.calls == []


def test_translate_text_reports_glossary_usage_from_cache_hits(tmp_path):
    translator = FakeTranslator()
    glossary = Glossary(
        [
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE, required=True),
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo", required=True),
            GlossaryEntry("AntiPattern", GLOSSARY_RULE_FORBIDDEN),
        ]
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        cache_key = CacheKey(text="Keep me", language_pair=LanguagePair(source="en", target="pt"))
        cache.set(cache_key, "observer uses the Game Loop and AntiPattern")

        result = translate_text("Keep me", translator, cache, "en", "pt", glossary=glossary)

    assert result.glossary_usage.applied_terms == {"Game Loop": 1, "Observer": 1}
    assert result.glossary_usage.required_terms_present == {"Game Loop": 1, "Observer": 1}
    assert result.glossary_usage.forbidden_terms_found == {"AntiPattern": 1}


def test_translate_html_accumulates_glossary_usage(tmp_path):
    html = "<html><body><p>Keep me</p><p>Keep me</p></body></html>"
    translator = FakeTranslator()
    glossary = Glossary(
        [
            GlossaryEntry("Mantenha-me", GLOSSARY_RULE_PRESERVE, required=True),
        ]
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _output, stats = translate_html(html, translator, cache, "en", "pt", glossary=glossary)

    assert stats.glossary_usage.applied_terms == {"Mantenha-me": 2}
    assert stats.glossary_usage.required_terms_present == {"Mantenha-me": 2}
