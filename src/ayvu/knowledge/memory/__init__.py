"""Translation memory: fuzzy similarity is here; approved memory is the approved subpackage."""
from __future__ import annotations

from .fuzzy import TranslationMemoryMatch, similarity_score

__all__ = [
    "TranslationMemoryMatch", "similarity_score",
]
