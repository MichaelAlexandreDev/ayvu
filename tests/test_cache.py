import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ayvu.cache import CacheImportError, CacheKey, TranslationCache, text_hash
from ayvu.kernel import LanguagePair


def _cache_key(text: str, source: str = "en", target: str = "pt") -> CacheKey:
    return CacheKey(text=text, language_pair=LanguagePair(source=source, target=target))


def _set_created_at(cache: TranslationCache, key: CacheKey, created_at: str) -> None:
    cache.connection.execute(
        """
        UPDATE translations
        SET created_at = ?
        WHERE source_lang = ?
          AND target_lang = ?
          AND original_text_hash = ?
        """,
        (
            created_at,
            key.language_pair.source,
            key.language_pair.target,
            key.original_text_hash,
        ),
    )
    cache.connection.commit()


def test_cache_round_trip(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        key = _cache_key("Hello")

        assert cache.get(key) is None
        cache.set(key, "Olá")
        assert cache.get(key) == "Olá"


def test_cache_is_language_specific(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        cache.set(_cache_key("Hello", "en", "pt"), "Olá")

        assert cache.get(_cache_key("Hello", "en", "es")) is None


def test_cache_accepts_concurrent_writes_from_separate_connections(tmp_path):
    cache_path = tmp_path / "translations.sqlite"

    def write_entry(index: int) -> None:
        with TranslationCache(cache_path) as cache:
            cache.set(_cache_key(f"Text {index}"), f"T{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_entry, range(24)))

    with TranslationCache(cache_path) as cache:
        rows = cache.summary(source_lang="en", target_lang="pt")

    assert len(rows) == 1
    assert rows[0].count == 24


def test_cache_writable_check_does_not_persist_probe(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        cache.verify_writable()

        assert cache.get(_cache_key("__ayvu_cache_write_check__", "ayvu", "ayvu")) is None


def test_fuzzy_candidates_filters_by_pair_and_length(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        cache.set(_cache_key("I have a cat.", "en", "pt"), "Eu tenho um gato.")
        cache.set(_cache_key("I have a dog.", "en", "pt"), "Eu tenho um cachorro.")
        cache.set(_cache_key("A much much longer sentence that is far away in length.", "en", "pt"), "Longo")
        cache.set(_cache_key("I have a cat.", "en", "es"), "Tengo un gato.")

        candidates = cache.fuzzy_candidates(
            language_pair=LanguagePair(source="en", target="pt"),
            text="I have a bat.",
            min_ratio=0.6,
            max_candidates=10,
        )

    originals = [original for original, _ in candidates]
    translations = [translated for _, translated in candidates]
    assert "I have a cat." in originals
    assert "I have a dog." in originals
    assert "A much much longer sentence that is far away in length." not in originals
    assert "Tengo un gato." not in translations


def test_fuzzy_candidates_respects_limit(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        for index in range(5):
            cache.set(_cache_key(f"Sentence number {index} here.", "en", "pt"), f"T{index}")

        candidates = cache.fuzzy_candidates(
            language_pair=LanguagePair(source="en", target="pt"),
            text="Sentence number 9 here.",
            min_ratio=0.6,
            max_candidates=2,
        )

    assert len(candidates) == 2


def test_cache_summary_groups_by_language_pair_with_dates(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        first = _cache_key("Hello", "en", "pt")
        second = _cache_key("World", "en", "pt")
        third = _cache_key("Hola", "es", "pt")
        cache.set(first, "Olá")
        cache.set(second, "Mundo")
        cache.set(third, "Olá")
        _set_created_at(cache, first, "2024-01-01 10:00:00")
        _set_created_at(cache, second, "2024-01-03 10:00:00")
        _set_created_at(cache, third, "2024-01-02 10:00:00")

        rows = cache.summary()

    assert len(rows) == 2
    assert rows[0].source_lang == "en"
    assert rows[0].target_lang == "pt"
    assert rows[0].count == 2
    assert rows[0].first_created_at == "2024-01-01 10:00:00"
    assert rows[0].last_created_at == "2024-01-03 10:00:00"
    assert rows[1].source_lang == "es"
    assert rows[1].count == 1


def test_cache_delete_entries_uses_filters(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    with TranslationCache(cache_path) as cache:
        old_entry = _cache_key("Hello", "en", "pt")
        new_entry = _cache_key("World", "en", "pt")
        other_pair = _cache_key("Hello", "en", "es")
        cache.set(old_entry, "Olá")
        cache.set(new_entry, "Mundo")
        cache.set(other_pair, "Hola")
        _set_created_at(cache, old_entry, "2024-01-01 10:00:00")
        _set_created_at(cache, new_entry, "2024-02-01 10:00:00")
        _set_created_at(cache, other_pair, "2024-01-01 10:00:00")

        report = cache.delete_entries(source_lang="en", target_lang="pt", before="2024-02-01 00:00:00")

        assert report.matched == 1
        assert report.deleted == 1
        assert cache.get(old_entry) is None
        assert cache.get(new_entry) == "Mundo"
        assert cache.get(other_pair) == "Hola"


def test_cache_export_and_import_json(tmp_path):
    source_path = tmp_path / "source.sqlite"
    export_path = tmp_path / "cache-export.json"
    first = _cache_key("Hello", "en", "pt")
    second = _cache_key("World", "en", "pt")

    with TranslationCache(source_path) as cache:
        cache.set(first, "Olá")
        cache.set(second, "Mundo")
        _set_created_at(cache, first, "2024-01-01 10:00:00")
        _set_created_at(cache, second, "2024-01-02 10:00:00")
        export_report = cache.export_json(export_path, source_lang="en", target_lang="pt")

    assert export_report.exported == 2
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [entry["original_text"] for entry in payload["entries"]] == ["Hello", "World"]

    target_path = tmp_path / "target.sqlite"
    with TranslationCache(target_path) as cache:
        import_report = cache.import_json(export_path)
        skipped_report = cache.import_json(export_path)

        assert import_report.imported == 2
        assert import_report.skipped == 0
        assert cache.get(first) == "Olá"
        assert skipped_report.imported == 0
        assert skipped_report.skipped == 2


def test_cache_import_json_can_replace_existing_entries(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    export_path = tmp_path / "cache-export.json"
    key = _cache_key("Hello", "en", "pt")
    export_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "source_lang": "en",
                        "target_lang": "pt",
                        "original_text_hash": key.original_text_hash,
                        "original_text": "Hello",
                        "translated_text": "Oi",
                        "created_at": "2024-01-01 10:00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with TranslationCache(cache_path) as cache:
        cache.set(key, "Olá")

        report = cache.import_json(export_path, replace=True)

        assert report.imported == 0
        assert report.replaced == 1
        assert cache.get(key) == "Oi"


def test_cache_import_json_rejects_hash_mismatch(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    export_path = tmp_path / "bad-export.json"
    export_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "source_lang": "en",
                        "target_lang": "pt",
                        "original_text_hash": text_hash("different text"),
                        "original_text": "Hello",
                        "translated_text": "Olá",
                        "created_at": "2024-01-01 10:00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with TranslationCache(cache_path) as cache:
        with pytest.raises(CacheImportError, match="invalid original_text_hash"):
            cache.import_json(export_path)
