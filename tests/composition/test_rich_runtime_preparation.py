from __future__ import annotations
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
import ayvu.composition.builders.translate_project as composition
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.knowledge.cache import PutIfAbsentState
from ayvu.knowledge.cache.schema import SchemaState, classify_cache
from tests.support.cache_repository_v2_support import entry
from ayvu.kernel import TranslationOptions, JobFailureFamily
from ayvu.translation import BookRefused
from tests.support.rich_runtime_support import PAIR, runtime


@pytest.mark.parametrize("changes,message", [({"workers": 2}, "--workers 1"),
    ({"require_full_cache": True}, "--require-full-cache")])
def test_each_unsupported_option_is_the_actual_gate_before_cache_and_provider(tmp_path, changes, message):
    calls = []
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR, **changes), prepare=lambda: calls.append("provider"))
    assert subject.preflight().code == "preflight_failed"
    assert message in subject.preflight_error.summary
    assert calls == [] and not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


@pytest.mark.parametrize("problem,code", [("platform", "cache_platform_unsupported"), ("foreign", "cache_foreign"),
                                         ("v1", "cache_foreign"), ("symlink", "unsafe_cache_path")])
def test_cache_is_refused_before_preparation_and_errors_are_sanitized(tmp_path, monkeypatch, problem, code):
    path = composition.v2_cache_path(tmp_path / "cache.sqlite")
    calls = []
    if problem == "platform":
        monkeypatch.setattr("ayvu.knowledge.cache.schema._supported", lambda _: False)
    elif problem == "foreign":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE private(x)")
    elif problem == "v1":
        with TranslationCache(path) as cache:
            cache.set(CacheKey("private source", PAIR), "private result")
    else:
        path.symlink_to(tmp_path / "other")
    subject = runtime(tmp_path, prepare=lambda: calls.append("provider"))
    before = path.read_bytes() if path.exists() else None
    failure = subject.preflight()
    assert failure.code == code and subject.preflight_error.detail == code
    assert calls == [] and str(path) not in str(subject.preflight_error)
    if before is not None:
        assert path.read_bytes() == before


def test_prepare_cache_is_complete_before_provider_preparation_and_preserves_v1(tmp_path):
    path = tmp_path / "cache.sqlite"
    with TranslationCache(path) as cache:
        cache.set(CacheKey("private source", PAIR), "private result")
    before = path.read_bytes()
    called = []
    def prepare():
        assert classify_cache(composition.v2_cache_path(path)) is SchemaState.V2
        assert path.read_bytes() == before
        called.append(True)
        return composition.LegacyResources(None)
    assert runtime(tmp_path, prepare=prepare).preflight() is None
    assert called == [True] and path.read_bytes() == before


def test_concurrent_preparation_and_put_if_absent_at_the_production_sibling(tmp_path):
    # Keep the inode stable here so this test isolates lock contention. Initial
    # creation and refusal of substituted files are separate repository contracts.
    assert runtime(tmp_path).preflight() is None
    barrier = Barrier(2)
    def prepare(_):
        subject = runtime(tmp_path)
        barrier.wait()
        return subject, subject.preflight()
    with ThreadPoolExecutor(max_workers=2) as workers:
        prepared = list(workers.map(prepare, range(2)))
    # Read-only classification may itself overlap the other exclusive preparation.
    # Contention refuses safely; a retry after both finish must prepare the same v2.
    for subject, failure in prepared:
        if failure is not None:
            assert failure.code == "cache_busy"
            assert subject.preflight() is None
    caches = [subject._cache for subject, _ in prepared]
    item = entry()
    def put(index):
        barrier.wait()
        return caches[index].put_if_absent(item).state
    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(put, range(2)))
    assert set(states) == {PutIfAbsentState.INSERTED, PutIfAbsentState.IDEMPOTENT}


def test_existing_empty_v1_and_its_mode_are_untouched_without_hardlinks(tmp_path, monkeypatch):
    import errno
    import os
    import stat
    legacy = tmp_path / "cache.sqlite"
    legacy.touch(mode=0o640)
    legacy.chmod(0o640)
    def no_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "fixture")
    monkeypatch.setattr(os, "link", no_link)
    previous = os.umask(0o077)
    try:
        assert runtime(tmp_path).preflight() is None
    finally:
        os.umask(previous)
    assert legacy.read_bytes() == b"" and stat.S_IMODE(legacy.stat().st_mode) == 0o640
    assert stat.S_IMODE(composition.v2_cache_path(legacy).stat().st_mode) == 0o600


def test_a_held_preparation_lock_refuses_before_provider(tmp_path):
    from ayvu.knowledge.cache.schema import _application_lock
    first = runtime(tmp_path)
    assert first.preflight() is None
    calls = []
    second = runtime(tmp_path, prepare=lambda: calls.append("provider"))
    with _application_lock(composition.v2_cache_path(tmp_path / "cache.sqlite"), exclusive=True):
        failure = second.preflight()
    assert failure.code == "cache_busy" and calls == []


def test_rich_preparation_keeps_the_book_policy_refusal_code(tmp_path):
    def prepare():
        raise BookRefused("encrypted_content")
    subject = runtime(tmp_path, prepare=prepare)
    failure = subject.preflight()
    assert failure.family is JobFailureFamily.INPUT and failure.code == "encrypted_content"
    assert isinstance(subject.preflight_error, BookRefused)


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_offline_preflight_uses_only_explicit_local_preparation(tmp_path, monkeypatch, workers):
    from ayvu.translation.preflight import run_local_translation_preflight
    from tests.support.rich_cache_only_support import _book, offline_guard, forbidden
    source = _book(tmp_path / "source.epub")
    local = lambda: run_local_translation_preflight(epub_path=source, glossary_path=None, language_pair=PAIR,
        translator_name="libretranslate", url="http://localhost:5000", timeout=30, retries=0,
        requests_per_second=None, retry_backoff=0.5, retry_backoff_max=8)
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR, cache_only=True, workers=workers), prepare=forbidden)
    subject._prepare_offline = local
    with offline_guard(monkeypatch):
        assert subject.preflight() is None
    assert subject._prepared.translator is None
    assert type(subject._cache).__name__ == "ReadOnlyExactCache"
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


def test_offline_resources_bind_executor_without_online_factories(tmp_path, monkeypatch):
    from ayvu.translation import RuntimeContext
    from tests.support.rich_cache_only_support import warm_units, offline_guard, forbidden, document
    doc = document("Hello")
    _, env, _ = warm_units(composition.v2_cache_path(tmp_path / "cache.sqlite"), (doc,))
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR, cache_only=True),
        prepare=forbidden, label="local-model-2026")
    subject._prepare_offline = lambda: composition.LegacyResources(None)
    monkeypatch.setattr("ayvu.composition.builders.rich_workers.SqliteProvenanceRepository",
        lambda *args, **kwargs: env.provenance)
    with offline_guard(monkeypatch):
        assert subject.preflight() is None
        with subject.resources(RuntimeContext(env.job, None, env.events)) as resources:
            result = resources.documents.execute(doc)
            result = result.executions[0]
    assert result.status == "cache" and result.translated == "PT:Hello"


def test_offline_mapping_shares_rich_normalization_and_preserves_missing_source():
    from types import SimpleNamespace
    from ayvu.translation import UnitExecution
    from ayvu.composition.builders.rich_document_executor import map_offline_execution
    unit = SimpleNamespace(content=SimpleNamespace(kind=SimpleNamespace(name="paragraph")))
    cached = UnitExecution("PT:Hello\r\nworld\x00", "cache", "source", "translated")
    assert map_offline_execution(unit, cached).reconstruction_text == "PT:Hello\nworld"
    missing = UnitExecution("original\r\n", "missing", "original", "original", missing_text="original")
    assert map_offline_execution(unit, missing) is missing
