from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import LanguagePair


SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    original_text_hash TEXT NOT NULL,
    original_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_lang, target_lang, original_text_hash)
);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CacheError(Exception):
    pass


class CacheExportError(CacheError):
    pass


class CacheImportError(CacheError):
    pass


@dataclass(frozen=True, repr=False)
class CacheKey:
    text: str
    language_pair: LanguagePair

    def __repr__(self) -> str:
        return "CacheKey(<redacted>)"

    @property
    def original_text_hash(self) -> str:
        return text_hash(self.text)


@dataclass(frozen=True)
class CacheSummaryRow:
    source_lang: str
    target_lang: str
    count: int
    first_created_at: str
    last_created_at: str


@dataclass(frozen=True, repr=False)
class CacheEntry:
    source_lang: str
    target_lang: str
    original_text_hash: str
    original_text: str
    translated_text: str
    created_at: str

    def __repr__(self) -> str:
        return "CacheEntry(<redacted>)"

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "CacheEntry":
        if not isinstance(value, dict):
            raise CacheImportError(f"entry #{index} must be an object")

        required_fields = (
            "source_lang",
            "target_lang",
            "original_text_hash",
            "original_text",
            "translated_text",
            "created_at",
        )
        values: dict[str, str] = {}
        for field in required_fields:
            raw_value = value.get(field)
            if not isinstance(raw_value, str):
                raise CacheImportError(f"entry #{index} field {field!r} must be a string")
            values[field] = raw_value

        expected_hash = text_hash(values["original_text"])
        if values["original_text_hash"] != expected_hash:
            raise CacheImportError(f"entry #{index} has an invalid original_text_hash")

        return cls(**values)

    def to_mapping(self) -> dict[str, str]:
        return {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "original_text_hash": self.original_text_hash,
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CacheDeleteReport:
    matched: int
    deleted: int


@dataclass(frozen=True, repr=False)
class CacheExportReport:
    path: Path
    exported: int

    def __repr__(self) -> str:
        return f"CacheExportReport(exported={self.exported})"


@dataclass(frozen=True)
class CacheImportReport:
    imported: int
    skipped: int
    replaced: int

    @property
    def total_read(self) -> int:
        return self.imported + self.skipped + self.replaced


class TranslationCache:
    EXPORT_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute(SCHEMA)
        self.connection.commit()

    def get(self, key: CacheKey) -> str | None:
        row = self.connection.execute(
            """
            SELECT translated_text
            FROM translations
            WHERE source_lang = ?
              AND target_lang = ?
              AND original_text_hash = ?
            """,
            (key.language_pair.source, key.language_pair.target, key.original_text_hash),
        ).fetchone()
        return row[0] if row else None

    def set(self, key: CacheKey, translated_text: str) -> None:
        self.connection.execute(
            """
            INSERT INTO translations (
                source_lang,
                target_lang,
                original_text_hash,
                original_text,
                translated_text
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_lang, target_lang, original_text_hash)
            DO UPDATE SET translated_text = excluded.translated_text
            """,
            (
                key.language_pair.source,
                key.language_pair.target,
                key.original_text_hash,
                key.text,
                translated_text,
            ),
        )
        self.connection.commit()

    def fuzzy_candidates(
        self,
        *,
        language_pair: LanguagePair,
        text: str,
        min_ratio: float,
        max_candidates: int,
    ) -> tuple[tuple[str, str], ...]:
        """Fetch candidate ``(original_text, translated_text)`` pairs for fuzzy reuse.

        Only entries of the same language pair are considered, narrowed by a
        coarse length band derived from ``min_ratio`` so the whole table is
        never loaded into memory. Precise similarity scoring happens on this
        bounded candidate set in the translation memory layer. Results are
        ordered by length closeness and capped at ``max_candidates``.
        """
        low, high = _length_band(len(text), min_ratio)
        rows = self.connection.execute(
            """
            SELECT original_text, translated_text
            FROM translations
            WHERE source_lang = ?
              AND target_lang = ?
              AND length(original_text) BETWEEN ? AND ?
            ORDER BY ABS(length(original_text) - ?), id
            LIMIT ?
            """,
            (
                language_pair.source,
                language_pair.target,
                low,
                high,
                len(text),
                max(1, max_candidates),
            ),
        ).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def summary(
        self,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        before: str | None = None,
    ) -> tuple[CacheSummaryRow, ...]:
        where_sql, params = _cache_filter_sql(source_lang=source_lang, target_lang=target_lang, before=before)
        rows = self.connection.execute(
            f"""
            SELECT
                source_lang,
                target_lang,
                COUNT(*) AS entry_count,
                MIN(created_at) AS first_created_at,
                MAX(created_at) AS last_created_at
            FROM translations
            {where_sql}
            GROUP BY source_lang, target_lang
            ORDER BY source_lang, target_lang
            """,
            params,
        ).fetchall()

        return tuple(
            CacheSummaryRow(
                source_lang=row[0],
                target_lang=row[1],
                count=row[2],
                first_created_at=row[3],
                last_created_at=row[4],
            )
            for row in rows
        )

    def entries(
        self,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        before: str | None = None,
    ) -> tuple[CacheEntry, ...]:
        where_sql, params = _cache_filter_sql(source_lang=source_lang, target_lang=target_lang, before=before)
        rows = self.connection.execute(
            f"""
            SELECT
                source_lang,
                target_lang,
                original_text_hash,
                original_text,
                translated_text,
                created_at
            FROM translations
            {where_sql}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()

        return tuple(CacheEntry(*row) for row in rows)

    def delete_entries(
        self,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        before: str | None = None,
    ) -> CacheDeleteReport:
        where_sql, params = _cache_filter_sql(source_lang=source_lang, target_lang=target_lang, before=before)
        matched = self.connection.execute(
            f"SELECT COUNT(*) FROM translations {where_sql}",
            params,
        ).fetchone()[0]
        cursor = self.connection.execute(
            f"DELETE FROM translations {where_sql}",
            params,
        )
        self.connection.commit()
        return CacheDeleteReport(matched=matched, deleted=cursor.rowcount)

    def export_json(
        self,
        path: str | Path,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        before: str | None = None,
        overwrite: bool = False,
    ) -> CacheExportReport:
        output_path = Path(path)
        if output_path.exists() and not overwrite:
            raise CacheExportError(f"export file already exists: {output_path}")

        entries = self.entries(source_lang=source_lang, target_lang=target_lang, before=before)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "version": self.EXPORT_VERSION,
                    "entries": [entry.to_mapping() for entry in entries],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return CacheExportReport(path=output_path, exported=len(entries))

    def import_json(self, path: str | Path, *, replace: bool = False) -> CacheImportReport:
        input_path = Path(path)
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CacheImportError(f"could not read cache export: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CacheImportError(f"invalid JSON export: {exc}") from exc

        entries = _parse_cache_export(payload)
        imported = 0
        skipped = 0
        replaced = 0

        with self.connection:
            for entry in entries:
                exists = self.connection.execute(
                    """
                    SELECT 1
                    FROM translations
                    WHERE source_lang = ?
                      AND target_lang = ?
                      AND original_text_hash = ?
                    """,
                    (entry.source_lang, entry.target_lang, entry.original_text_hash),
                ).fetchone()

                if exists and not replace:
                    skipped += 1
                    continue

                if exists:
                    self.connection.execute(
                        """
                        UPDATE translations
                        SET original_text = ?,
                            translated_text = ?,
                            created_at = ?
                        WHERE source_lang = ?
                          AND target_lang = ?
                          AND original_text_hash = ?
                        """,
                        (
                            entry.original_text,
                            entry.translated_text,
                            entry.created_at,
                            entry.source_lang,
                            entry.target_lang,
                            entry.original_text_hash,
                        ),
                    )
                    replaced += 1
                    continue

                self.connection.execute(
                    """
                    INSERT INTO translations (
                        source_lang,
                        target_lang,
                        original_text_hash,
                        original_text,
                        translated_text,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.source_lang,
                        entry.target_lang,
                        entry.original_text_hash,
                        entry.original_text,
                        entry.translated_text,
                        entry.created_at,
                    ),
                )
                imported += 1

        return CacheImportReport(imported=imported, skipped=skipped, replaced=replaced)

    def verify_writable(self) -> None:
        original_text = "__ayvu_cache_write_check__"
        self.connection.execute("SAVEPOINT ayvu_cache_write_check")
        try:
            self.connection.execute(
                """
                INSERT INTO translations (
                    source_lang,
                    target_lang,
                    original_text_hash,
                    original_text,
                    translated_text
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "ayvu",
                    "ayvu",
                    text_hash(original_text),
                    original_text,
                    original_text,
                ),
            )
        except sqlite3.Error:
            self._rollback_write_check(ignore_errors=True)
            raise

        self._rollback_write_check(ignore_errors=False)

    def _rollback_write_check(self, ignore_errors: bool) -> None:
        if ignore_errors:
            with suppress(sqlite3.Error):
                self.connection.execute("ROLLBACK TO SAVEPOINT ayvu_cache_write_check")
            with suppress(sqlite3.Error):
                self.connection.execute("RELEASE SAVEPOINT ayvu_cache_write_check")
            return

        self.connection.execute("ROLLBACK TO SAVEPOINT ayvu_cache_write_check")
        self.connection.execute("RELEASE SAVEPOINT ayvu_cache_write_check")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TranslationCache":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _length_band(length: int, min_ratio: float) -> tuple[int, int]:
    """Length window a candidate must fall in to possibly reach ``min_ratio``.

    For the normalized indel ratio used by the memory, the best score two
    strings of lengths ``a`` and ``b`` can reach is ``2*min(a, b)/(a + b)``.
    Solving that bound for the candidate length yields a symmetric window
    ``[t*L/(2 - t), L*(2 - t)/t]`` that lets the SQL layer discard hopeless
    candidates before any scoring.
    """
    if length <= 0 or min_ratio <= 0.0:
        return 0, 2_000_000_000
    ratio = min(min_ratio, 1.0)
    low = math.floor(length * ratio / (2.0 - ratio))
    high = math.ceil(length * (2.0 - ratio) / ratio)
    return max(0, low), high


def _cache_filter_sql(
    *,
    source_lang: str | None,
    target_lang: str | None,
    before: str | None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if source_lang:
        clauses.append("source_lang = ?")
        params.append(source_lang)
    if target_lang:
        clauses.append("target_lang = ?")
        params.append(target_lang)
    if before:
        clauses.append("created_at < ?")
        params.append(before)

    if not clauses:
        return "", ()

    return "WHERE " + " AND ".join(clauses), tuple(params)


def _parse_cache_export(payload: Any) -> tuple[CacheEntry, ...]:
    if not isinstance(payload, dict):
        raise CacheImportError("cache export must be a JSON object")
    if payload.get("version") != TranslationCache.EXPORT_VERSION:
        raise CacheImportError(f"unsupported cache export version: {payload.get('version')!r}")

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise CacheImportError("cache export entries must be a list")

    return tuple(CacheEntry.from_mapping(entry, index) for index, entry in enumerate(raw_entries, start=1))
