"""A cache file in the v1 format, written with the frozen ``LEGACY_SCHEMA`` and plain ``sqlite3``.

Ayvu still reads these files (cache administration, the optional legacy compatibility, the v2 migration
census), so tests create them on disk. Nothing here is production code: the v1 runtime that wrote them is gone.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ayvu.kernel import LanguagePair
from ayvu.knowledge.cache import LEGACY_SCHEMA


def text_hash(text: str) -> str:
    """The v1 row key: the SHA-256 of the original text, in hexadecimal."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class CacheKey:
    text: str
    language_pair: LanguagePair

    def __repr__(self) -> str:
        return "CacheKey(<redacted>)"

    @property
    def original_text_hash(self) -> str:
        return text_hash(self.text)


class LegacyCacheFile:
    """Opens or creates a v1 cache file and reads or writes its rows, as the v1 runtime did."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute(LEGACY_SCHEMA)
        self.connection.commit()

    def get(self, key: CacheKey) -> str | None:
        row = self.connection.execute(
            "SELECT translated_text FROM translations WHERE source_lang = ? AND target_lang = ? AND original_text_hash = ?",
            (key.language_pair.source, key.language_pair.target, key.original_text_hash)).fetchone()
        return row[0] if row else None

    def set(self, key: CacheKey, translated_text: str) -> None:
        self.connection.execute(
            "INSERT INTO translations (source_lang, target_lang, original_text_hash, original_text, translated_text) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_lang, target_lang, original_text_hash) "
            "DO UPDATE SET translated_text = excluded.translated_text",
            (key.language_pair.source, key.language_pair.target, key.original_text_hash, key.text, translated_text))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LegacyCacheFile":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class LegacyCachePort:
    """The file behind the text pipeline's exact-cache port (``get``/``set`` by text and language pair)."""

    def __init__(self, cache: LegacyCacheFile) -> None:
        self._cache = cache

    def get(self, text: str, language_pair: LanguagePair) -> str | None:
        return self._cache.get(CacheKey(text, language_pair))

    def set(self, text: str, language_pair: LanguagePair, translated: str) -> None:
        self._cache.set(CacheKey(text, language_pair), translated)
