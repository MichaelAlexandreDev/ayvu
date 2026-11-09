"""Operational interchange boundaries with independently valid inputs."""
from dataclasses import replace
import io
from types import SimpleNamespace

import pytest

from ayvu import cache_identity, cache_interchange, cache_repository
from ayvu.cache_admin import CacheAdminError
from ayvu.cache_identity import CACHE_ENTRY_V2_LIMITS, CanonicalValue, exact_entry_key
from ayvu.cache_interchange import iter_records, validate_v2_entry
from ayvu.cache_repository import _digest, _entry_digest, SQLiteExactCache
from ayvu.cache_schema import V2_COLUMNS, participating_connection
from test_cache_repository_v2 import entry
from test_translation_cache_v2 import make_plan


def test_nested_value_depth_eight_is_parsed_and_nine_hits_depth_limit():
    # At depth eight the descriptor's type is invalid, but the parser got past
    # its depth guard. One extra array must fail specifically at that guard.
    def raw(arrays):
        value = b'[' * arrays + b'0' + b']' * arrays
        return b'{"version":2,"namespaces":[{"kind":' + value + b',"entries":[]}]}'

    with pytest.raises(CacheAdminError) as at_limit:
        list(iter_records(io.BytesIO(raw(5)), SimpleNamespace(depth=8)))
    assert at_limit.value.code == 'malformed_namespace'
    with pytest.raises(CacheAdminError) as over_limit:
        list(iter_records(io.BytesIO(raw(6)), SimpleNamespace(depth=8)))
    assert over_limit.value.code == 'structure_too_deep'


def test_canonical_identity_limit_with_valid_complete_identity(tmp_path, monkeypatch):
    operation = make_plan().semantic_operation
    parameters = tuple((f'k{i:02d}' + 'k' * 990, CanonicalValue('string', 'v' * 1000)) for i in range(29))
    key_name = 'z' * 900
    candidate = replace(operation, generation_parameters=parameters + ((key_name, CanonicalValue('string', 'v')),))
    remaining = CACHE_ENTRY_V2_LIMITS.canonical_identity - len(candidate.canonical_payload())
    operation = replace(operation, generation_parameters=parameters + ((key_name, CanonicalValue('string', 'v' * (remaining + 1))),))
    canonical = operation.canonical_payload()
    assert len(canonical) == 65536
    base = entry()
    key = exact_entry_key(base.key.scope, base.key.value_kind, _digest(canonical), base.source_text)
    path = tmp_path / 'cache.sqlite'
    SQLiteExactCache(path).put_if_absent(replace(base, key=key, canonical_identity=canonical))
    with participating_connection(path) as connection:
        row = connection.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
    descriptor = dict(kind='v2', scope_kind=row[0], scope_id=row[1])
    good = {name: value.decode() if isinstance(value, bytes) else value for name, value in zip(V2_COLUMNS[2:], row[2:])}
    assert validate_v2_entry(descriptor, good) == row

    # Construct N+1 from the same real DTO. Temporarily relax only the two
    # dependency ceilings to prove the row is otherwise complete and valid.
    with monkeypatch.context() as relaxed:
        relaxed.setattr(cache_identity, 'MAX_CANONICAL_PAYLOAD_BYTES', 65537)
        relaxed.setattr(cache_repository, 'LIMITS', replace(CACHE_ENTRY_V2_LIMITS, canonical_identity=65537))
        longer = replace(operation, generation_parameters=parameters + ((key_name, CanonicalValue('string', 'v' * (remaining + 2))),))
        payload = longer.canonical_payload()
        assert len(payload) == 65537
        longer_key = exact_entry_key(base.key.scope, base.key.value_kind, _digest(payload), base.source_text)
        bad = dict(good, canonical_identity=payload.decode(), semantic_fingerprint=longer_key.semantic_fingerprint)
        bad['entry_digest'] = _entry_digest(longer_key, bad['source_text'], bad['value'], payload,
                                            bad['metadata'].encode(), bad['provenance'].encode(),
                                            bad['created_at'], bad['provenance_digest'])
        assert validate_v2_entry(descriptor, bad)
    with pytest.raises(CacheAdminError) as over_limit:
        validate_v2_entry(descriptor, bad)
    assert over_limit.value.code == 'malformed_entry'


class RepeatedEntriesStream:
    """Generate one compact envelope on demand; retain at most one 64 KiB batch."""
    prefix = b'{"version":2,"namespaces":[{"kind":"v2","scope_kind":"work","scope_id":"scope","entries":['
    suffix = b']}]}'

    def __init__(self, entries):
        self.remaining = entries
        self.pending = self.prefix
        self.first = True
        self.finished = False
        self.calls = self.largest = 0

    def read(self, size):
        assert 0 < size <= 65536
        self.calls += 1
        self.largest = max(self.largest, size)
        while len(self.pending) < size and self.remaining:
            count = min(self.remaining, max(1, (size - len(self.pending)) // 3))
            self.pending += (b'{}' if self.first else b',{}') + b',{}' * (count - 1)
            self.first = False
            self.remaining -= count
        if not self.remaining and not self.finished:
            self.pending += self.suffix
            self.finished = True
        result, self.pending = self.pending[:size], self.pending[size:]
        return result


@pytest.mark.parametrize('count', [5_000_000, 5_000_001])
def test_five_million_entry_limit_streamed(count, monkeypatch):
    # Isolate the envelope counter from expensive row validation: every parsed
    # placeholder is passed through the entry-validation seam, never retained.
    validated = 0
    def validate_placeholder(descriptor, value, limits):
        nonlocal validated
        assert value == {} and descriptor['kind'] == 'v2'
        validated += 1
    monkeypatch.setattr(cache_interchange, '_entry', validate_placeholder)
    stream = RepeatedEntriesStream(count)
    records = iter_records(stream, SimpleNamespace())
    descriptor, value = next(records)
    assert descriptor['kind'] == 'v2' and value is None
    received = 0
    if count == 5_000_000:
        for _ in records:
            received += 1
    else:
        with pytest.raises(CacheAdminError) as exceeded:
            for _ in records:
                received += 1
        assert exceeded.value.code == 'too_many_entries'
    assert received == validated == 5_000_000
    assert stream.calls > 200 and stream.largest == 65536
