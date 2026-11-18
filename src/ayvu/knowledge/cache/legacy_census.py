"""Counts an old (v1) cache's texts without changing it, for the notice that they are not reused.

REF-18 step 5 (path B): rich translations never read the v1 table, so a user who translated with v1
is told that those texts stay where they are and that the book is translated again. The file is
opened with SQLite's read-only URI mode: it is never created, migrated or written. A journal or WAL
file beside it means that it is in use or was interrupted, and then it is reported as unreadable
without being opened, so nothing next to it changes either.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_BESIDE = ("-journal", "-wal", "-shm")
_HEADER = b"SQLite format 3\x00"
_WAL_VERSION = 2  # header bytes 18 and 19; a read-only open of a WAL database may need to create -shm


@dataclass(frozen=True)
class LegacyCacheCensus:
    path: Path
    texts: int | None  # None: the file is there but could not be read

    @property
    def readable(self) -> bool:
        return self.texts is not None


def legacy_cache_census(path: Path, *, target: str, source: str | None = None) -> LegacyCacheCensus | None:
    """The v1 texts into ``target`` (from ``source`` when given), or None when there is nothing to say.

    Nothing to say means no file, an empty file, no v1 table, or no text for the languages.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        if any(path.with_name(path.name + suffix).exists() for suffix in _BESIDE):
            return LegacyCacheCensus(path, None)
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError:
        return LegacyCacheCensus(path, None)
    if len(header) < 100 or not header.startswith(_HEADER) or _WAL_VERSION in (header[18], header[19]):
        return LegacyCacheCensus(path, None)
    try:
        texts = _count(path, target, source)
    except sqlite3.Error:
        return LegacyCacheCensus(path, None)
    return None if not texts else LegacyCacheCensus(path, texts)


def _count(path: Path, target: str, source: str | None) -> int | None:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'translations'").fetchone() is None:
            return None
        if source is None:
            row = connection.execute("SELECT COUNT(*) FROM translations WHERE target_lang = ?", (target,)).fetchone()
        else:
            row = connection.execute("SELECT COUNT(*) FROM translations WHERE source_lang = ? AND target_lang = ?",
                                     (source, target)).fetchone()
        return row[0]
    finally:
        connection.close()
