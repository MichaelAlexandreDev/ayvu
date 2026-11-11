import re
from collections import Counter
import pytest
from bs4 import BeautifulSoup
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.knowledge.memory.fuzzy import TranslationMemory
from ayvu.knowledge.glossary.rules import (
    GLOSSARY_RULE_FORBIDDEN,
    GLOSSARY_RULE_PRESERVE,
    GLOSSARY_RULE_TRANSLATE,
    Glossary,
    GlossaryEntry,
)
from ayvu.v1.html_translate import (
    _protected_terms_are_intact,
    apply_reviewed_html,
    extract_visible_text,
    translate_html,
    translate_text,
)
from ayvu.providers.libretranslate.client import Translator
from ayvu.kernel import LanguagePair, TranslationMemoryOptions

class DamagingMarkerTranslator(Translator):
    def __init__(self, damage="spaces", fallback="translate"):
        self.damage = damage
        self.fallback = fallback
        self.calls = []

    def translate(self, text, source, target):
        self.calls.append(text)
        tokens = re.findall(r"__AYVU_PROTECTED_\d+__", text)
        if tokens:
            if self.damage == "spaces":
                return text.replace("__AYVU_PROTECTED_", "AYVU PROTECTED ").replace("__", "")
            if self.damage == "drop":
                return text.replace(tokens[0], "")
            if self.damage == "duplicate":
                return text + tokens[0]
            if self.damage == "unknown":
                return text + "__AYVU_PROTECTED_999__"
            if self.damage == "reverse":
                replacements = iter(reversed(tokens))
                return re.sub(r"__AYVU_PROTECTED_\d+__", lambda _: next(replacements), text)
        if self.fallback == "error":
            raise RuntimeError("synthetic failure")
        if self.fallback == "empty":
            return ""
        if self.fallback == "marker":
            return "AYVU PROTECTED 9"
        for original, translated in {"Read": "Leia", "with": "com", "care": "cuidado", "Visit": "Visite", "and": "e"}.items():
            text = text.replace(original, translated)
        return text


class FakeTranslator(Translator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls.append(text)
        replacements = {
            "Any programming book with": "Qualquer livro de programação com",
            "Patterns": "Patterns",
            "in its name.": "no nome.",
            "Keep me": "Mantenha-me",
        }
        return replacements.get(text, f"PT:{text}")


def _seed_memory(cache, apply_threshold=0.9, suggest_threshold=0.7):
    cache.set(CacheKey(text="I have a cat.", language_pair=LanguagePair("en", "pt")), "Eu tenho um gato.")
    return TranslationMemory(
        cache,
        TranslationMemoryOptions(apply_threshold=apply_threshold, suggest_threshold=suggest_threshold),
    )


def _resolver(reviewed: dict[int, str], seen: list[tuple[int, str, str]] | None = None):
    def resolve(index: int, kind: str, original: str):
        if seen is not None:
            seen.append((index, kind, original))
        return reviewed.get(index)

    return resolve


class IdentityOr(Translator):
    def __init__(self, replace=("world", "mundo")):
        self.replace = replace
        self.sent = []

    def translate(self, text, source, target):
        self.sent.append(text)
        return text.replace(*self.replace)


def run_markup(tmp_path, html, translator=None, **options):
    translator = translator or IdentityOr()
    with TranslationCache(tmp_path / "markup.sqlite") as cache:
        output, stats = translate_html(html, translator, cache, "en", "pt", **options)
    return output.decode(), stats, translator


NAMESPACED = (
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:xlink="http://www.w3.org/1999/xlink"><body>'
    '<p>{}</p></body></html>'
)
