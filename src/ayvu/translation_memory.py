from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .cache import TranslationCache
from .domain import LanguagePair, TranslationMemoryOptions


@dataclass(frozen=True, repr=False)
class TranslationMemoryMatch:
    """Best fuzzy match found for a piece of text.

    ``score`` is the normalized similarity in the ``[0, 1]`` range. ``applied``
    is ``True`` when the score reached the apply threshold, meaning the stored
    translation may be reused directly; otherwise the match is only a
    suggestion to be surfaced for review.
    """

    original: str
    translated: str
    score: float
    applied: bool

    def __repr__(self) -> str:
        return f"TranslationMemoryMatch(score={self.score!r}, applied={self.applied})"


class TranslationMemory:
    """Fuzzy reuse of stored translations for similar source text.

    Heavy similarity scoring lives here, on top of the candidate set the cache
    fetches from disk. The cache stays responsible for SQL and storage; this
    layer only ranks candidates and applies the configured thresholds.
    """

    def __init__(self, cache: TranslationCache, options: TranslationMemoryOptions) -> None:
        self._cache = cache
        self._options = options

    def lookup(self, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None:
        core = text.strip()
        if not core:
            return None

        candidates = self._cache.fuzzy_candidates(
            language_pair=language_pair,
            text=core,
            min_ratio=self._options.suggest_threshold,
            max_candidates=self._options.max_candidates,
        )
        if not candidates:
            return None

        best = process.extractOne(core, [original for original, _ in candidates], scorer=fuzz.ratio)
        if best is None:
            return None

        _matched, raw_score, index = best
        score = raw_score / 100.0
        if score < self._options.suggest_threshold:
            return None

        original, translated = candidates[index]
        return TranslationMemoryMatch(
            original=original,
            translated=translated,
            score=score,
            applied=self._options.applies(score),
        )
