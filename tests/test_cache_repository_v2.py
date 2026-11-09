from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest

from ayvu.cache_identity import (
    CACHE_ENTRY_V2_LIMITS as LIMITS, CacheAuthorizationPolicy, CacheIdentityError,
    CacheLookupState, CacheProvenanceMetadata, ExactCacheEntry, PutIfAbsentState,
    TRANSLATION_STAGE_AGGREGATE, exact_entry_key,
)
from ayvu.cache_repository import SQLiteExactCache
from test_translation_cache_v2 import Env, PROJECT, make_plan


def entry(source="source", value="translation", metadata=b"{}"):
    plan = make_plan()
    operation = plan.semantic_operation
    key = exact_entry_key(CacheAuthorizationPolicy(PROJECT).resolve(), TRANSLATION_STAGE_AGGREGATE,
                          operation.fingerprint, source)
    return ExactCacheEntry(key, value, CacheProvenanceMetadata("translate", plan.producer, operation.pipeline_revision),
                           source, operation.canonical_payload(), metadata)


def test_real_adapter_document_translation_and_local_lookup(tmp_path):
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    first = Env(cache=cache)
    from test_translation_cache_v2 import document
    first.translate(document("hello"))
    second = Env(cache=cache)
    result = second.translate(document("hello"))
    assert result.units[0].source.value == "exact_cache"
    assert not second.provider.requests


def test_concurrent_compare_and_insert_never_replaces_and_clock_only_for_winner(tmp_path):
    barrier = Barrier(2)
    guard = Lock()
    calls = []
    def clock():
        with guard:
            calls.append(1)
        return datetime(2026, 11, 5, 12, tzinfo=UTC)
    cache = SQLiteExactCache(tmp_path / "cache.sqlite", clock=clock)
    a = entry(value="first")
    b = replace(a, value="second")
    def put(item):
        barrier.wait()
        return cache.put_if_absent(item).state
    with ThreadPoolExecutor(max_workers=2) as executor:
        states = list(executor.map(put, (a, b)))
    assert set(states) == {PutIfAbsentState.INSERTED, PutIfAbsentState.CONFLICT}
    assert len(calls) == 1
    assert cache.put_if_absent(a if cache.lookup(a.key).value == a.value else b).state == PutIfAbsentState.IDEMPOTENT
    assert len(calls) == 1


@pytest.mark.parametrize("field,limit", [("source_text", LIMITS.source_text), ("value", LIMITS.value)])
def test_text_limits_exact_and_over(tmp_path, field, limit):
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    if field == "source_text":
        exact, over = entry(source="s" * limit), entry(source="s" * (limit + 1))
    else:
        exact, over = entry(value="x" * limit), entry(source="other", value="x" * (limit + 1))
    assert cache.put_if_absent(exact).state == PutIfAbsentState.INSERTED
    assert cache.put_if_absent(over).state == PutIfAbsentState.NOT_CACHEABLE


def test_metadata_boundary_and_digest_mismatch(tmp_path):
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    exact = b'{"x":[' + b'0,' * 32763 + b'10]}'
    assert len(exact) == LIMITS.metadata
    assert cache.put_if_absent(entry(metadata=exact)).state == PutIfAbsentState.INSERTED
    over = exact.replace(b'10]}', b'100]}')
    assert cache.put_if_absent(entry(source="other", metadata=over)).state == PutIfAbsentState.NOT_CACHEABLE
    mismatch = replace(entry(source="different"), source_text="tampered")
    assert cache.put_if_absent(mismatch).state == PutIfAbsentState.NOT_CACHEABLE


@pytest.mark.parametrize("column", ["created_at", "provenance", "value", "entry_digest", "provenance_digest"])
def test_read_detects_corruption_and_redacts(tmp_path, column):
    from ayvu.cache_schema import participating_connection
    path = tmp_path / "secret.sqlite"
    cache = SQLiteExactCache(path)
    item = entry(source="CANARY-SOURCE", value="CANARY-VALUE")
    assert cache.put_if_absent(item).state == PutIfAbsentState.INSERTED
    with participating_connection(path) as connection:
        connection.execute(f"UPDATE cache_entries_v2 SET {column}=?", (b"broken" if column == "provenance" else "broken",))
        connection.commit()
    with pytest.raises(CacheIdentityError) as error:
        cache.lookup(item.key)
    assert "CANARY" not in repr(error.value) + repr(cache) + repr(item)


def test_canonical_and_provenance_byte_limits_and_timestamp():
    import hashlib
    from ayvu.cache_identity import CanonicalValue
    from ayvu.cache_repository import _validate_payload
    base = entry()
    operation = make_plan().semantic_operation
    parameters = tuple((f"p{i:03}", CanonicalValue("string", "x" * 1024)) for i in range(56))
    def canonical(n):
        return replace(operation, generation_parameters=(*parameters, ("p056", CanonicalValue("string", "x" * n)))).canonical_payload()
    exact = canonical(LIMITS.canonical_identity - len(canonical(1)) + 1)
    assert len(exact) == LIMITS.canonical_identity
    over = exact.replace(b'"p056"', b'"p056x"')
    def key_for(payload):
        return replace(base.key, semantic_fingerprint="sha256:" + hashlib.sha256(payload).hexdigest())
    provenance = b'{"operation":"' + b'x' * (LIMITS.provenance - len(b'{"operation":"","producer":{},"pipeline_revision":{}}')) + b'","producer":{},"pipeline_revision":{}}'
    assert len(provenance) == LIMITS.provenance
    assert _validate_payload(key_for(exact), base.source_text, base.value, exact, b'{}', provenance,
                             "2026-11-05T12:00:00.000000Z")
    assert not _validate_payload(key_for(over), base.source_text, base.value, over, b'{}', provenance)
    over_provenance = provenance.replace(b'"operation":"', b'"operation":"x', 1)
    assert not _validate_payload(key_for(exact), base.source_text, base.value, exact, b'{}', over_provenance)
    for invalid in ("2026-11-05T12:00:00Z", "2026-11-05T12:00:00.000000+00:00", "2026-02-30T12:00:00.000000Z"):
        assert not _validate_payload(key_for(exact), base.source_text, base.value, exact, b'{}', provenance, invalid)


def test_oversized_provider_result_is_returned_without_cache_write(tmp_path):
    from test_translation_cache_v2 import document, Provider
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    plan = make_plan()
    provider = Provider(plan, respond=lambda _: "x" * (LIMITS.value + 1))
    environment = Env(plan, cache=cache, provider=provider)
    result = environment.translate(document("hello"))
    assert len(result.units[0].text) == LIMITS.value + 1
    assert cache.lookup(entry(source="hello").key).state == CacheLookupState.MISS


def test_same_key_metadata_and_provenance_conflict(tmp_path):
    from ayvu.provenance import ProducerIdentity
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    original = entry()
    assert cache.put_if_absent(original).state == PutIfAbsentState.INSERTED
    assert cache.put_if_absent(original).state == PutIfAbsentState.IDEMPOTENT
    assert cache.put_if_absent(replace(original, metadata=b'{"changed":true}')).state == PutIfAbsentState.CONFLICT
    producer = ProducerIdentity("provider@1", "other@1", "adapter@1", "pipeline@1")
    changed = replace(original, provenance=replace(original.provenance, producer=producer))
    assert cache.put_if_absent(changed).state == PutIfAbsentState.CONFLICT
    assert cache.lookup(original.key).value == original.value


@pytest.mark.parametrize("metadata", [b'{"prompt":"CANARY"}', b'{"api_key":1}', b'{"x":"CANARY"}', b'{"x":{"secret":1}}'])
def test_metadata_refuses_content_and_credentials(tmp_path, metadata):
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    assert cache.put_if_absent(entry(metadata=metadata)).state == PutIfAbsentState.NOT_CACHEABLE


def test_identity_rejects_fabricated_nested_fields(tmp_path):
    import hashlib
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    base = entry()
    injected = base.canonical_identity.replace(b'"route_hop",', b'"route_hop","secret":"CANARY",', 1)
    assert injected != base.canonical_identity
    key = replace(base.key, semantic_fingerprint="sha256:" + hashlib.sha256(injected).hexdigest())
    assert cache.put_if_absent(replace(base, key=key, canonical_identity=injected)).state == PutIfAbsentState.NOT_CACHEABLE


@pytest.mark.parametrize("different_provenance", [False, True])
def test_simultaneous_identical_and_provenance_writes(tmp_path, different_provenance):
    from ayvu.provenance import ProducerIdentity
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    first = entry()
    second = first
    if different_provenance:
        producer = ProducerIdentity("provider@1", "other@1", "adapter@1", "pipeline@1")
        second = replace(first, provenance=replace(first.provenance, producer=producer))
    gate = Barrier(2)
    def write(item):
        gate.wait()
        return cache.put_if_absent(item).state
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(write, (first, second)))
    assert sorted(state.value for state in states) == sorted([
        PutIfAbsentState.INSERTED.value,
        (PutIfAbsentState.CONFLICT if different_provenance else PutIfAbsentState.IDEMPOTENT).value,
    ])
    assert cache.lookup(first.key).value == first.value


def test_migrated_legacy_exact_and_fuzzy_are_isolated_from_v2_workers(tmp_path):
    import sqlite3
    from ayvu.cache import TranslationCache
    from ayvu.domain import LanguagePair, TranslationMemoryOptions
    from ayvu.html_translate import _LegacyV1Cache
    from ayvu.translation_memory import TranslationMemory
    path = tmp_path / "cache.sqlite"
    pair = LanguagePair("en", "pt")
    with TranslationCache(path) as legacy:
        facade = _LegacyV1Cache(legacy)
        facade.set("hello", pair, "LEGACY-CANARY")
        assert facade.get("hello", pair) == "LEGACY-CANARY"
        assert TranslationMemory(legacy, TranslationMemoryOptions()).lookup("hello", pair).translated == "LEGACY-CANARY"
    cache = SQLiteExactCache(path)
    item = entry(source="hello")
    assert cache.lookup(item.key).state == CacheLookupState.MISS
    assert cache.resolve_compatible(item.key.scope, item.key.value_kind, item.key.source_content_digest, limit=256) == ()
    assert cache.put_if_absent(item).state == PutIfAbsentState.INSERTED
    assert cache.lookup(item.key).value == item.value
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT translated_text FROM translations").fetchall() == [("LEGACY-CANARY",)]
        assert db.execute("SELECT COUNT(*) FROM cache_entries_v2").fetchone() == (1,)


def test_production_cache_only_never_discovers_or_opens_network(tmp_path, monkeypatch):
    import socket
    from ayvu.cache_identity import CacheOnlyRequest, PinnedExecutionIdentityReference
    from test_translation_cache_v2 import document
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    Env(cache=cache).translate(document("hello", key="fill.txt"))
    calls = []
    def forbidden(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError("unexpected_transport_or_discovery")
        return call
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden(name))
    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, forbidden(name))
    class ForbiddenProvider:
        execute = staticmethod(forbidden("provider"))
        discover = staticmethod(forbidden("discovery"))
    class ForbiddenMemory:
        lookup = staticmethod(forbidden("fuzzy"))
    pinned = Env(cache=cache, provider=ForbiddenProvider(), memory=ForbiddenMemory()).translate(
        document("hello", key="pinned.txt"),
        cache_only=CacheOnlyRequest.pinned(PinnedExecutionIdentityReference("job:unresolved")))
    assert pinned.units[0].source.value == "cache_only_missing"
    compatible = Env(cache=cache, provider=ForbiddenProvider(), memory=ForbiddenMemory()).translate(
        document("hello", key="compatible.txt"), cache_only=CacheOnlyRequest.resolve_compatible())
    assert compatible.units[0].source.value == "exact_cache"
    assert calls == []


def test_canonical_oversize_dto_is_not_cacheable(tmp_path):
    import hashlib
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    base = entry()
    oversized = base.canonical_identity + b' ' * (LIMITS.canonical_identity + 1 - len(base.canonical_identity))
    assert len(oversized) == LIMITS.canonical_identity + 1
    key = replace(base.key, semantic_fingerprint="sha256:" + hashlib.sha256(oversized).hexdigest())
    assert cache.put_if_absent(replace(base, key=key, canonical_identity=oversized)).state == PutIfAbsentState.NOT_CACHEABLE


def test_timestamp_exact_length_boundary():
    from ayvu.cache_repository import _valid_timestamp
    exact = "2026-11-05T12:00:00.000000Z"
    assert len(exact) == LIMITS.timestamp and _valid_timestamp(exact)
    over = exact + "Z"
    assert len(over) == LIMITS.timestamp + 1 and not _valid_timestamp(over)


def test_timestamp_uses_four_digit_year_and_normalizes_timezone():
    from datetime import timedelta, timezone
    from ayvu.cache_repository import _timestamp, _valid_timestamp
    assert _timestamp(lambda: datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00.000000Z"
    assert _valid_timestamp("0001-01-01T00:00:00.000000Z")
    assert _timestamp(lambda: datetime(2026, 11, 5, 9, tzinfo=timezone(timedelta(hours=-3)))) == "2026-11-05T12:00:00.000000Z"


def test_separate_utf8_ceilings_can_be_used_together_without_truncation(tmp_path):
    from ayvu.cache_schema import participating_connection
    cache = SQLiteExactCache(tmp_path / "cache.sqlite")
    source = "á" * (LIMITS.source_text // 2)
    metadata = b'{"x":[0]}'
    metadata += b' ' * (LIMITS.metadata - len(metadata))
    item = entry(source=source, value="v" * LIMITS.value, metadata=metadata)
    assert cache.put_if_absent(item).state == PutIfAbsentState.INSERTED
    assert cache.lookup(item.key).value == item.value
    with participating_connection(tmp_path / "cache.sqlite") as conn:
        row = conn.execute("SELECT source_text,metadata FROM cache_entries_v2").fetchone()
    assert row == (source, metadata)
    over = entry(source=source + "x", value=item.value, metadata=metadata)
    assert len(over.source_text.encode("utf-8")) == LIMITS.source_text + 1
    assert cache.put_if_absent(over).state == PutIfAbsentState.NOT_CACHEABLE
    assert cache.put_if_absent(replace(item, value=item.value + "x")).state == PutIfAbsentState.NOT_CACHEABLE


def test_identity_uses_the_normative_entry_limit():
    from ayvu.cache_identity import MAX_CANONICAL_PAYLOAD_BYTES
    assert MAX_CANONICAL_PAYLOAD_BYTES == LIMITS.canonical_identity == 64 * 1024


def test_legacy_cache_dto_representations_omit_operational_content():
    from pathlib import Path
    from ayvu.cache import CacheEntry, CacheExportReport, CacheKey, text_hash
    from ayvu.domain import LanguagePair
    source, value = "CANARY-SOURCE", "CANARY-VALUE"
    key = CacheKey(source, LanguagePair("en", "pt"))
    legacy = CacheEntry("en", "pt", text_hash(source), source, value, "2026-11-05")
    exported = CacheExportReport(Path("/CANARY-PATH/private"), 1)
    assert "CANARY" not in repr((key, legacy, exported))
    # Only the explicit interchange projection contains sensitive text.
    assert legacy.to_mapping()["original_text"] == source
    assert legacy.to_mapping()["translated_text"] == value
