import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from ayvu.cache_admin import CacheAdminError, validate_payload
from ayvu.cache_legacy_stream import iter_legacy_entries


def row(text='olá😀'):
    return dict(source_lang='en', target_lang='pt', original_text_hash=hashlib.sha256(text.encode()).hexdigest(), original_text=text, translated_text='ok', created_at='2026')


def read(raw, *, request=None, max_bytes=512 * 1024 * 1024):
    seen = {}
    def key_seen(oid, digest):
        if digest in seen.setdefault(oid, set()): raise CacheAdminError('malformed_json')
        seen[oid].add(digest)
    def closed(oid): seen.pop(oid, None)
    return list(iter_legacy_entries(io.BytesIO(raw), key_seen, closed, request, max_bytes))


@pytest.mark.parametrize('payload', [
    {'version': 1, 'entries': [row()]},
    {'entries': [row()], 'unknown': {'a': [1, True, None, {'foo': 'bar'}]}, 'version': 1},
    {'version': 1, 'unknown': [[], {}, 1e100], 'entries': [{**row(), 'unknown': {'value': ['x', False]}}]},
    {'version': 1, 'entries': []},
])
def test_matches_legacy_acceptance(payload):
    raw = json.dumps(payload, ensure_ascii=True).encode()
    assert validate_payload(raw) == len(payload['entries'])
    assert read(raw) == [{key: value for key, value in item.items() if key in row()} for item in payload['entries']]


@pytest.mark.parametrize('raw', [
    b'{"version":1,"entries":[],"version":1}',
    b'{"version":1,"entries":[],"unknown":{"x":1,"x":2}}',
    b'{"version":1,"entries":[],"unknown":{"\\u0078":1,"x":2}}',
    b'{"version":1,"entries":[],"unknown":[NaN]}',
    b'{"version":1,"entries":[],"unknown":[1,]}',
    b'{"version":1,"entries":[],"unknown":"\\q"}',
    b'{"version":1,"entries":[],"unknown":"\xff"}',
    b'{"version":1,"entries":[]}junk',
    b'{"version":2,"entries":[]}',
    b'{"version":1,"entries":[{}]}',
])
def test_rejects_same_invalid_inputs(raw):
    with pytest.raises(CacheAdminError): validate_payload(raw)
    with pytest.raises(CacheAdminError): read(raw)


def test_escaped_unicode_duplicate_keys_and_unknown_lone_surrogate():
    raw = b'{"version":1,"entries":[],"unknown":{"\\ud83d\\ude00":1,"\xf0\x9f\x98\x80":2}}'
    with pytest.raises(CacheAdminError): read(raw)
    raw = b'{"version":1,"entries":[],"unknown":"\\ud800"}'
    assert validate_payload(raw) == 0
    assert read(raw) == []


def test_large_unknown_string_streams_in_bounded_reads():
    class Source:
        def __init__(self): self.prefix = b'{"unknown":"'; self.remaining = 8 * 1024 * 1024; self.tail = b'","entries":[],"version":1}'; self.requests = []
        def read(self, size):
            self.requests.append(size)
            assert 0 < size <= 65536
            if self.prefix: data, self.prefix = self.prefix, b''; return data
            if self.remaining:
                n = min(size, self.remaining); self.remaining -= n; return b'x' * n
            data, self.tail = self.tail, b''; return data
    stream = Source()
    assert list(iter_legacy_entries(stream, lambda *_: None, lambda *_: None)) == []
    assert len(stream.requests) > 128


def test_byte_boundary_and_cancel():
    raw = json.dumps({'version': 1, 'entries': [row()]}).encode()
    assert len(read(raw, max_bytes=len(raw))) == 1
    with pytest.raises(CacheAdminError, match='file_too_large'): read(raw, max_bytes=len(raw)-1)
    class Cancel:
        def checkpoint(self, point): return True if point == 'cache_between_entries' else None
    with pytest.raises(CacheAdminError, match='cancelled'):
        read(raw, request=SimpleNamespace(cancellation=Cancel()))


def test_known_text_boundaries_and_surrogate_validation():
    for length in (1_000_000, 1_000_001):
        raw = json.dumps({'version': 1, 'entries': [row('a' * length)]}).encode()
        if length == 1_000_000:
            assert len(read(raw)[0]['original_text']) == length
        else:
            with pytest.raises(CacheAdminError, match='text_too_large'): read(raw)
    broken = row('x')
    broken['translated_text'] = '\ud800'
    raw = json.dumps({'version': 1, 'entries': [broken]}).encode()
    with pytest.raises(CacheAdminError, match='malformed_entry'): read(raw)


def test_nesting_boundary_and_late_version_rejection():
    for depth in (16, 17):
        raw = b'{"version":1,"entries":[],"other":' + b'[' * (depth - 1) + b'0' + b']' * (depth - 1) + b'}'
        if depth == 16: assert read(raw) == []
        else:
            with pytest.raises(CacheAdminError, match='structure_too_deep'): read(raw)
    raw = json.dumps({'entries': [row()], 'version': 2}).encode()
    iterator = iter_legacy_entries(io.BytesIO(raw), lambda *_: None, lambda *_: None)
    assert next(iterator) == row()
    with pytest.raises(CacheAdminError, match='unsupported_version'): next(iterator)


def test_utf8_multibyte_characters_split_across_input_reads():
    raw = json.dumps({'version': 1, 'entries': [row('😀é')], 'unknown': '😀é'}, ensure_ascii=False).encode()
    class Tiny(io.BytesIO):
        def read(self, size): return super().read(min(size, 1))
    assert list(iter_legacy_entries(Tiny(raw), lambda *_: None, lambda *_: None)) == [row('😀é')]


@pytest.mark.parametrize('encoding,bom', [
    ('utf-8', b'\xef\xbb\xbf'), ('utf-16-le', b''), ('utf-16-be', b''),
    ('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff'),
    ('utf-32-le', b''), ('utf-32-be', b''),
    ('utf-32-le', b'\xff\xfe\x00\x00'), ('utf-32-be', b'\x00\x00\xfe\xff'),
])
def test_legacy_detects_encoded_json_and_original_byte_limit(encoding, bom):
    raw = bom + json.dumps({'entries': [row('日本語😀')], 'version': 1}, ensure_ascii=False).encode(encoding)
    assert validate_payload(raw) == 1
    class Tiny(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 65536
            return super().read(min(size, 1))
    assert list(iter_legacy_entries(Tiny(raw), lambda *_: None, lambda *_: None, max_bytes=len(raw))) == [row('日本語😀')]
    with pytest.raises(CacheAdminError, match='file_too_large'):
        list(iter_legacy_entries(Tiny(raw), lambda *_: None, lambda *_: None, max_bytes=len(raw) - 1))


def test_unknown_raw_surrogate_utf16_accepted_but_known_rejected():
    raw = '{"version":1,"entries":[],"unknown":"\ud800"}'.encode('utf-16-le', 'surrogatepass')
    assert validate_payload(raw) == 0
    assert read(raw) == []
    bad = row('x'); bad['translated_text'] = '\ud800'
    raw = json.dumps({'version': 1, 'entries': [bad]}, ensure_ascii=False).encode('utf-16-le', 'surrogatepass')
    with pytest.raises(CacheAdminError, match='malformed_entry'): read(raw)


def test_cancel_during_unknown_value_batches_without_any_entries():
    from types import SimpleNamespace
    class Cancel:
        calls = 0
        def checkpoint(self, point):
            assert point == 'cache_between_entries'
            self.calls += 1
            return True if self.calls == 2 else None
    token = Cancel()
    raw = b'{"version":1,"unknown":"' + b'x' * 200_000 + b'","entries":[]}'
    with pytest.raises(CacheAdminError, match='cancelled'):
        read(raw, request=SimpleNamespace(cancellation=token))
    assert token.calls == 2


@pytest.mark.parametrize('payload', [[], None, True, 12, 'text',
    {'version': 2, 'entries': [{}]}, {'entries': [{}], 'version': 2},
    {'version': 2, 'entries': 'invalid'}, {'version': 1, 'entries': [{}]},
    {'version': 1, 'entries': [{**row(), 'source_lang': 1}]},
    {'version': 1, 'entries': [{**row(), 'source_lang': 'x' * 257}]},
])
def test_error_priority_matches_existing_validator(payload):
    raw = json.dumps(payload).encode()
    with pytest.raises(CacheAdminError) as old: validate_payload(raw)
    with pytest.raises(CacheAdminError) as new: read(raw)
    assert old.value.code == new.value.code


def test_entry_limit_precedes_invalid_entry_and_version_precedes_limit():
    for version, code in ((1, 'too_many_entries'), (2, 'unsupported_version')):
        raw = json.dumps({'entries': [{}, row()], 'version': version}).encode()
        with pytest.raises(CacheAdminError, match=code):
            list(iter_legacy_entries(io.BytesIO(raw), lambda *_: None, lambda *_: None, max_entries=1))


def test_syntax_error_after_invalid_entry_takes_precedence():
    raw = b'{"version":1,"entries":[{},],"unknown":0}'
    with pytest.raises(CacheAdminError) as old: validate_payload(raw)
    with pytest.raises(CacheAdminError) as new: read(raw)
    assert old.value.code == new.value.code == 'malformed_json'
