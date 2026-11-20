import pytest
from ayvu.translation.preflight import PreflightError, run_translation_preflight
from ayvu.providers.libretranslate.client import RequestRateLimiter, RoutedTranslator, TranslatorError, TranslatorLanguage
from ayvu.kernel import LanguagePair

class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append((text, source, target))
        return text


class FailingTranslator:
    def translate(self, _text: str, _source: str, _target: str) -> str:
        raise TranslatorError("language pair is not available")


class TranslatorWithLanguages:
    timeout, retries, rate_limiter = 1.0, 0, RequestRateLimiter()

    def __init__(self, languages: tuple[TranslatorLanguage, ...]) -> None:
        self._languages = languages
        self.translate_calls: list[tuple[str, str, str]] = []
        self.list_calls = 0

    def translate(self, text: str, source: str, target: str) -> str:
        self.translate_calls.append((text, source, target))
        return text

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        self.list_calls += 1
        return self._languages


class TranslatorWithFailingLanguages:
    timeout, retries, rate_limiter = 1.0, 0, RequestRateLimiter()

    def translate(self, text: str, _source: str, _target: str) -> str:
        return text

    def list_languages(self) -> tuple[TranslatorLanguage, ...]:
        raise TranslatorError("connection refused")


def raise_bad_epub(_path: object) -> object:
    raise ValueError("bad epub")


def accept_cache(_path):
    """The cache check is the caller's port: the rich runtime prepares its own cache, so this one accepts."""


def _preflight(tmp_path, translator, monkeypatch, source="en", target="pt", url="http://localhost:5000"):
    monkeypatch.setattr("ayvu.translation.preflight.create_translator", lambda *_args, **_kwargs: translator)
    monkeypatch.setattr("ayvu.translation.preflight.inspect_epub", lambda _path: object())
    return run_translation_preflight(
        epub_path=tmp_path / "book.epub", cache_path=tmp_path / "cache.sqlite", glossary_path=None,
        translator_name="libretranslate", url=url, timeout=1.0, retries=0,
        language_pair=LanguagePair(source=source, target=target), dry_run=False, cache_check=accept_cache)
