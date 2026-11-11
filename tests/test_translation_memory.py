import pytest

from ayvu.cache import CacheKey, TranslationCache
from ayvu.translation_memory import TranslationMemory
from ayvu.kernel import LanguagePair, TranslationMemoryError, TranslationMemoryOptions


EN_PT = LanguagePair(source="en", target="pt")


def _seed(cache: TranslationCache, text: str, translated: str, pair: LanguagePair = EN_PT) -> None:
    cache.set(CacheKey(text=text, language_pair=pair), translated)


def test_lookup_applies_above_apply_threshold(tmp_path):
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _seed(cache, "I have a cat.", "Eu tenho um gato.")
        memory = TranslationMemory(
            cache, TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7)
        )

        match = memory.lookup("I have a cat!", EN_PT)

    assert match is not None
    assert match.applied is True
    assert match.translated == "Eu tenho um gato."
    assert match.score >= 0.9


def test_lookup_suggests_in_middle_band(tmp_path):
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _seed(cache, "I have a cat.", "Eu tenho um gato.")
        memory = TranslationMemory(
            cache, TranslationMemoryOptions(apply_threshold=0.97, suggest_threshold=0.6)
        )

        match = memory.lookup("I have a dog.", EN_PT)

    assert match is not None
    assert match.applied is False
    assert 0.6 <= match.score < 0.97


def test_lookup_returns_none_below_suggest_threshold(tmp_path):
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        # Same length keeps the candidate inside the length band, so only the
        # similarity gate can reject it.
        _seed(cache, "aaaaaaaaaa", "AAAA")
        memory = TranslationMemory(
            cache, TranslationMemoryOptions(apply_threshold=0.95, suggest_threshold=0.6)
        )

        assert memory.lookup("bbbbbbbbbb", EN_PT) is None


def test_lookup_is_language_pair_specific(tmp_path):
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _seed(cache, "I have a cat.", "Eu tenho um gato.")
        memory = TranslationMemory(
            cache, TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7)
        )

        assert memory.lookup("I have a cat!", LanguagePair(source="en", target="es")) is None


def test_lookup_empty_text_returns_none(tmp_path):
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _seed(cache, "I have a cat.", "Eu tenho um gato.")
        memory = TranslationMemory(cache, TranslationMemoryOptions())

        assert memory.lookup("   ", EN_PT) is None


def test_options_validate_rejects_suggest_above_apply():
    with pytest.raises(TranslationMemoryError):
        TranslationMemoryOptions(apply_threshold=0.8, suggest_threshold=0.9).validate()


def test_options_validate_rejects_out_of_range():
    with pytest.raises(TranslationMemoryError):
        TranslationMemoryOptions(apply_threshold=1.5).validate()
    with pytest.raises(TranslationMemoryError):
        TranslationMemoryOptions(suggest_threshold=0.0).validate()


def test_options_validate_accepts_defaults():
    TranslationMemoryOptions().validate()


def test_options_apply_and_suggest_predicates():
    options = TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7)

    assert options.applies(0.95) is True
    assert options.applies(0.9) is True
    assert options.suggests(0.8) is True
    assert options.suggests(0.9) is False
    assert options.suggests(0.6) is False
