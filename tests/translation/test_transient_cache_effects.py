"""Cold, ambiguous, corrupt and busy evidence never creates durable effects."""
import sqlite3

import pytest

from ayvu.kernel import LanguagePair, TranslationOptions
from ayvu.knowledge.cache import CacheIdentityError
from ayvu.knowledge.cache.schema import CacheSchemaError
from tests.support.transient_translation_support import _book, seed, execute, effect_guard, active_writer


def options(**changes):
    return TranslationOptions(LanguagePair("en", "pt"), dry_run=True, **changes)


@pytest.mark.parametrize("state", ["absent", "readonly", "corrupt", "busy", "journal"])
def test_reader_effects_and_storage_refusals(tmp_path, monkeypatch, state):
    source = _book(tmp_path / "book.epub")
    project, cache = seed(tmp_path, source, options())
    if state == "absent":
        cache.unlink()
    elif state == "readonly":
        cache.chmod(0o400)
    elif state == "corrupt":
        cache.write_bytes(b"corrupt")
    elif state == "journal":
        cache.with_name(cache.name + "-journal").write_bytes(b"persistent abandoned journal")
    if state == "busy":
        with active_writer(cache), effect_guard(monkeypatch, tmp_path), pytest.raises(CacheSchemaError, match="cache_busy"):
            execute(tmp_path, source, options(), project)
    elif state in {"corrupt", "journal"}:
        code = "cache_corrupt" if state == "corrupt" else "cache_recovery_required"
        with effect_guard(monkeypatch, tmp_path), pytest.raises(CacheSchemaError, match=code):
            execute(tmp_path, source, options(), project)
    else:
        with effect_guard(monkeypatch, tmp_path):
            report = execute(tmp_path, source, options(), project)
        assert report.texts_from_cache == (5 if state == "readonly" else 0)


def test_ambiguous_canonical_plans_count_as_missing(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    opts = options(cache_only=True)
    project, _ = seed(tmp_path, source, opts)
    seed(tmp_path, source, opts, project=project, plan_changes={"capabilities": "capfp:v1:" + "c" * 64})
    with effect_guard(monkeypatch, tmp_path):
        report = execute(tmp_path, source, opts, project)
    assert report.texts_missing == 5 and report.texts_from_cache == 0


def test_corrupt_payload_is_fatal_instead_of_a_missing_unit(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    project, cache = seed(tmp_path, source, options())
    with sqlite3.connect(cache) as connection:
        connection.execute("UPDATE cache_entries_v2 SET value='tampered'")
    with effect_guard(monkeypatch, tmp_path), pytest.raises(CacheIdentityError, match="cache_entry_corrupt"):
        execute(tmp_path, source, options(cache_only=True), project)
