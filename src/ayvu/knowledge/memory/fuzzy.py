"""Fuzzy similarity and the match value the text pipeline reports; the stores live in ``approved``."""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz


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


def similarity_score(left: str, right: str) -> float:
    """Deterministic Unicode Indel similarity; normalization is the caller's policy."""
    return fuzz.ratio(left, right) / 100.0
