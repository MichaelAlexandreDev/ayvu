"""The cache check translation preflight runs before the v1 runtime: open the v1 cache and prove it writable."""
from __future__ import annotations

from pathlib import Path

from .cache import TranslationCache


def verify_cache_writable(path: Path) -> None:
    with TranslationCache(path) as cache:
        cache.verify_writable()
