"""Translation memory. Fuzzy reuse over stored translations is here; approved memory is the approved
subpackage.
"""
from __future__ import annotations

from .fuzzy import TranslationMemory, TranslationMemoryMatch, similarity_score

__all__ = [
    "TranslationMemory", "TranslationMemoryMatch", "similarity_score",
]
