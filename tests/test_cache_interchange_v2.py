import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from ayvu.cache_admin import CacheAdminError
from ayvu.cache_interchange import iter_records, validate_v2_entry, write_envelope
from ayvu.cache_repository import SQLiteExactCache
from ayvu.cache_schema import V2_COLUMNS, participating_connection
from test_cache_repository_v2 import entry


def limits(**values):
    return SimpleNamespace(**values)


def legacy(text="abc"):
    return dict(source_lang="en", target_lang="pt", original_text_hash=hashlib.sha256(text.encode()).hexdigest(), original_text=text, translated_text="olá", created_at="2020")


def test_streaming_legacy_and_empty_section():
    descriptor = {"kind": "legacy-v1"}
    out = io.BytesIO()
    assert write_envelope(out, [(descriptor, iter([legacy()]))], limits()) == 1
    assert list(iter_records(io.BytesIO(out.getvalue()), limits())) == [(descriptor, None), (descriptor, legacy())]
    out = io.BytesIO()
    write_envelope(out, [(descriptor, iter(()))], limits())
    assert list(iter_records(io.BytesIO(out.getvalue()), limits())) == [(descriptor, None)]


def test_v2_round_trip_preserves_bytes_and_detects_corruption(tmp_path):
    path = tmp_path / "cache.sqlite"
    SQLiteExactCache(path).put_if_absent(entry(metadata=b'{ "x" : 1 }'))
    with participating_connection(path) as conn:
        row = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
    desc = dict(kind="v2", scope_kind=row[0], scope_id=row[1])
    item = {name: value.decode() if isinstance(value, bytes) else value for name, value in zip(V2_COLUMNS[2:], row[2:])}
    assert validate_v2_entry(desc, item) == row
    out = io.BytesIO()
    write_envelope(out, [(desc, [item])], limits())
    assert list(iter_records(io.BytesIO(out.getvalue()), limits()))[-1] == (desc, item)
    with pytest.raises(CacheAdminError):
        validate_v2_entry(desc, {**item, "value": "changed"})


@pytest.mark.parametrize("name", ["entries", "namespaces", "raw_input", "serialized_export"])
def test_limits_boundary(name):
    desc = {"kind": "legacy-v1"}
    data = io.BytesIO()
    write_envelope(data, [(desc, [legacy()])], limits())
    raw = data.getvalue()
    boundary = len(raw) if name in ("raw_input", "serialized_export") else 1
    if name == "serialized_export":
        write_envelope(io.BytesIO(), [(desc, [legacy()])], limits(**{name: boundary}))
        with pytest.raises(CacheAdminError): write_envelope(io.BytesIO(), [(desc, [legacy()])], limits(**{name: boundary - 1}))
    else:
        list(iter_records(io.BytesIO(raw), limits(**{name: boundary})))
        with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits(**{name: boundary - 1})))


@pytest.mark.parametrize("raw", [b'{"version":2,"namespaces":[],"extra":1}', b'{"version":2,"namespaces":[{"kind":"legacy-v1","entries":[{"a":1,"a":2}]}]}', b'{"version":2,"namespaces":[]}junk'])
def test_closed_and_malformed_input(raw):
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


@pytest.fixture
def v2row(tmp_path):
    path = tmp_path / "row.sqlite"
    SQLiteExactCache(path).put_if_absent(entry())
    with participating_connection(path) as conn:
        row = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
    desc = dict(kind="v2", scope_kind=row[0], scope_id=row[1])
    item = {name: value.decode() if isinstance(value, bytes) else value for name, value in zip(V2_COLUMNS[2:], row[2:])}
    return desc, item


@pytest.mark.parametrize("field", ["source_text", "value", "canonical_identity", "metadata", "provenance", "created_at"])
@pytest.mark.parametrize("direction", ["read", "write"])
def test_each_row_limit_exact_and_one_below(v2row, field, direction):
    desc, item = v2row
    fields = ("source_text", "value", "canonical_identity", "metadata", "provenance", "created_at")
    bounds = {"timestamp" if k == "created_at" else k: len(item[k].encode()) for k in fields}
    stream = io.BytesIO()
    write_envelope(stream, [(desc, [item])], limits())
    def invoke(row_limits):
        lim = limits(row_limits=SimpleNamespace(**row_limits))
        if direction == "read":
            return list(iter_records(io.BytesIO(stream.getvalue()), lim))
        return write_envelope(io.BytesIO(), [(desc, [item])], lim)
    invoke(bounds)
    key = "timestamp" if field == "created_at" else field
    with pytest.raises(CacheAdminError):
        invoke({**bounds, key: bounds[key] - 1})


@pytest.mark.parametrize("direction", ["read", "write"])
def test_scope_reference_boundary(v2row, direction):
    desc, item = v2row
    stream = io.BytesIO()
    write_envelope(stream, [(desc, [item])], limits())
    def invoke(n):
        if direction == "read": return list(iter_records(io.BytesIO(stream.getvalue()), limits(scope_reference=n)))
        return write_envelope(io.BytesIO(), [(desc, [item])], limits(scope_reference=n))
    size = len(desc["scope_id"].encode())
    invoke(size)
    with pytest.raises(CacheAdminError): invoke(size - 1)


@pytest.mark.parametrize("direction", ["read", "write"])
def test_depth_boundary(direction):
    desc = {"kind": "legacy-v1"}
    stream = io.BytesIO()
    write_envelope(stream, [(desc, [legacy()])], limits())
    def invoke(n):
        if direction == "read": return list(iter_records(io.BytesIO(stream.getvalue()), limits(depth=n)))
        return write_envelope(io.BytesIO(), [(desc, [legacy()])], limits(depth=n))
    invoke(5)
    with pytest.raises(CacheAdminError): invoke(4)


@pytest.mark.parametrize("raw", [
    b'{"version":3,"namespaces":[]}',
    b'{"version":true,"namespaces":[]}',
    b'{"version":2,"namespaces":[{"kind":"legacy-v1","scope_id":"x","entries":[]}]}',
    b'{"version":2,"namespaces":[{"kind":"legacy-v1","kind":"legacy-v1","entries":[]}]}',
    b'{"version":2,"namespaces":[{"kind":"legacy-v1","entries":[NaN]}]}',
    b'{"version":2,"namespaces":[]}\xff',
    b'{"version":2,"namespaces":[{"kind":"legacy-v1","entries":[]},{"kind":"legacy-v1","entries":[]}]}',
    b'{"namespaces":[],"version":2}',
    b'{"version":2,"namespaces":[{"entries":[],"kind":"legacy-v1"}]}',
])
def test_invalid_wire_contract(raw):
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


class InstrumentedInput(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.requests = []
    def read(self, size=-1):
        self.requests.append(size)
        assert 0 < size <= 65536
        return super().read(min(size, 13))


class InstrumentedOutput(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.writes = []
    def write(self, data):
        self.writes.append(len(data))
        return super().write(data)


def test_streams_process_rows_incrementally_and_unicode():
    desc = {"kind": "legacy-v1"}
    produced = []
    output = InstrumentedOutput()
    def rows():
        for i in range(50):
            produced.append(i)
            if i: assert len(output.writes) > i
            yield legacy("😀é" * 30 + str(i))
    assert write_envelope(output, [(desc, rows())], limits()) == 50
    inp = InstrumentedInput(output.getvalue())
    records = iter_records(inp, limits())
    assert next(records) == (desc, None)
    assert inp.tell() < len(output.getvalue())
    assert len(list(records)) == 50
    assert len(inp.requests) > 1


@pytest.mark.parametrize("direction", ["read", "write"])
def test_cancel_between_entries(direction):
    desc = {"kind": "legacy-v1"}
    class Cancel:
        calls = 0
        def checkpoint(self, point):
            assert point == "cache_between_entries"
            self.calls += 1
            return object() if self.calls == 2 else None
    cancel = Cancel()
    request = SimpleNamespace(cancellation=cancel)
    stream = io.BytesIO()
    write_envelope(stream, [(desc, [legacy(), legacy("other")])], limits())
    with pytest.raises(CacheAdminError) as error:
        if direction == "read": list(iter_records(io.BytesIO(stream.getvalue()), limits(), request))
        else: write_envelope(io.BytesIO(), [(desc, [legacy(), legacy("other")])], limits(), request)
    assert error.value.code == "cancelled"
    assert cancel.calls == 2


def test_large_canonical_identity_and_maximum_provenance_together(tmp_path):
    from dataclasses import replace
    from ayvu.cache_identity import CanonicalValue, CACHE_ENTRY_V2_LIMITS, exact_entry_key
    from ayvu.cache_repository import _digest, _entry_digest
    from test_translation_cache_v2 import make_plan
    operation = make_plan().semantic_operation
    parameters = tuple((f"k{i:02d}" + "k" * 990, CanonicalValue("string", "v" * 1000)) for i in range(29))
    extra_key = "z" * 900
    candidate = replace(operation, generation_parameters=parameters + ((extra_key, CanonicalValue("string", "v")),))
    remaining = CACHE_ENTRY_V2_LIMITS.canonical_identity - len(candidate.canonical_payload())
    operation = replace(operation, generation_parameters=parameters + ((extra_key, CanonicalValue("string", "v" * (remaining + 1))),))
    base = entry()
    key = exact_entry_key(base.key.scope, base.key.value_kind, operation.fingerprint, base.source_text)
    item = replace(base, key=key, canonical_identity=operation.canonical_payload())
    assert len(item.canonical_identity) == CACHE_ENTRY_V2_LIMITS.canonical_identity
    path = tmp_path / "large.sqlite"
    SQLiteExactCache(path).put_if_absent(item)
    with participating_connection(path) as conn:
        row = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
    values = dict(zip(V2_COLUMNS, row))
    provenance = values["provenance"] + b" " * (CACHE_ENTRY_V2_LIMITS.provenance - len(values["provenance"]))
    digest = _digest(provenance)
    values["provenance"] = provenance
    values["provenance_digest"] = digest
    values["entry_digest"] = _entry_digest(key, values["source_text"], values["value"], values["canonical_identity"], values["metadata"], provenance, values["created_at"], digest)
    desc = dict(kind="v2", scope_kind=values["scope_kind"], scope_id=values["scope_id"])
    wire = {k: values[k].decode() if isinstance(values[k], bytes) else values[k] for k in V2_COLUMNS[2:]}
    out = io.BytesIO()
    write_envelope(out, [(desc, [wire])], limits())
    assert list(iter_records(io.BytesIO(out.getvalue()), limits()))[-1] == (desc, wire)
    assert validate_v2_entry(desc, wire) == tuple(values[k] for k in V2_COLUMNS)


@pytest.mark.parametrize("field", ["source_text", "value", "metadata", "provenance", "created_at"])
def test_actual_row_ceilings_and_one_over(v2row, field):
    from ayvu.cache_identity import CACHE_ENTRY_V2_LIMITS, exact_entry_key
    from ayvu.cache_repository import _digest, _entry_digest
    desc, original = v2row
    base = entry()
    ceiling = getattr(CACHE_ENTRY_V2_LIMITS, "timestamp" if field == "created_at" else field)
    def make(n):
        item = dict(original)
        if field in ("source_text", "value"):
            item[field] = "x" * n
        elif field in ("metadata", "provenance"):
            item[field] = original[field] + " " * (n - len(original[field].encode()))
        else:
            item[field] = original[field] + "x" * (n - len(original[field]))
        key = exact_entry_key(base.key.scope, base.key.value_kind, item["semantic_fingerprint"], item["source_text"])
        item["source_digest"] = key.source_content_digest
        provenance = item["provenance"].encode()
        item["provenance_digest"] = _digest(provenance)
        item["entry_digest"] = _entry_digest(key, item["source_text"], item["value"], item["canonical_identity"].encode(), item["metadata"].encode(), provenance, item["created_at"], item["provenance_digest"])
        return item
    good, bad = make(ceiling), make(ceiling + 1)
    assert len(good[field].encode()) == ceiling
    output = io.BytesIO()
    write_envelope(output, [(desc, [good])], limits())
    assert list(iter_records(io.BytesIO(output.getvalue()), limits()))[-1] == (desc, good)
    with pytest.raises(CacheAdminError): write_envelope(io.BytesIO(), [(desc, [bad])], limits())
    raw = json.dumps({"version": 2, "namespaces": [{**desc, "entries": [bad]}]}, ensure_ascii=False).encode()
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


def test_actual_canonical_identity_one_over_rejected(v2row):
    from ayvu.cache_identity import CACHE_ENTRY_V2_LIMITS
    desc, item = v2row
    item["canonical_identity"] = " " * (CACHE_ENTRY_V2_LIMITS.canonical_identity + 1)
    with pytest.raises(CacheAdminError): validate_v2_entry(desc, item)


def test_actual_namespace_limit():
    # Empty v2 descriptors need no row or destination authority.
    def sections(n):
        return ((dict(kind="v2", scope_kind="work", scope_id=f"scope-{i}"), ()) for i in range(n))
    out = io.BytesIO()
    assert write_envelope(out, sections(10_000), limits()) == 0
    assert len(list(iter_records(io.BytesIO(out.getvalue()), limits()))) == 10_000
    with pytest.raises(CacheAdminError): write_envelope(io.BytesIO(), sections(10_001), limits())
    raw = out.getvalue()[:-2] + b',{"kind":"v2","scope_kind":"work","scope_id":"overflow","entries":[]}]}'
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


def test_actual_scope_reference_boundary():
    def sections(n):
        return [(dict(kind="v2", scope_kind="work", scope_id="x" * n), ())]
    out = io.BytesIO()
    write_envelope(out, sections(1024), limits())
    assert len(list(iter_records(io.BytesIO(out.getvalue()), limits()))) == 1
    with pytest.raises(CacheAdminError): write_envelope(io.BytesIO(), sections(1025), limits())
    raw = json.dumps({"version": 2, "namespaces": [{"kind": "v2", "scope_kind": "work", "scope_id": "x" * 1025, "entries": []}]}).encode()
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


@pytest.mark.parametrize("mutate", [
    lambda p: p["producer"].update(secret="credential"),
    lambda p: p["pipeline_revision"].update(secret="credential"),
    lambda p: p["producer"].update(provider="https://secret"),
    lambda p: p["pipeline_revision"].update(version=True),
    lambda p: p.update(operation="bad secret"),
])
def test_reject_nested_malformed_provenance_even_with_valid_digests(v2row, mutate):
    from ayvu.cache_repository import _digest, _entry_digest
    desc, item = v2row
    provenance = json.loads(item["provenance"])
    mutate(provenance)
    item["provenance"] = json.dumps(provenance, separators=(",", ":"))
    key = entry().key
    item["provenance_digest"] = _digest(item["provenance"].encode())
    item["entry_digest"] = _entry_digest(key, item["source_text"], item["value"], item["canonical_identity"].encode(), item["metadata"].encode(), item["provenance"].encode(), item["created_at"], item["provenance_digest"])
    with pytest.raises(CacheAdminError): validate_v2_entry(desc, item)


def test_standalone_v1_streams_as_legacy():
    desc = {"kind": "legacy-v1"}
    for entries in ([], [legacy("é😀"), legacy("two")]):
        raw = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False).encode()
        assert list(iter_records(InstrumentedInput(raw), limits())) == [(desc, None), *((desc, row) for row in entries)]
        with pytest.raises(CacheAdminError):
            list(iter_records(io.BytesIO(raw), limits(raw_input=len(raw) - 1)))
    raw = json.dumps({"version": 1, "entries": [legacy(), legacy("two")]}).encode()
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits(entries=1)))


@pytest.mark.parametrize("raw", [
    b'{"version":1,"entries":[],"extra":1}',
    b'{"version":1,"entries":[],"entries":[]}',
    b'{"version":1,"entries":[{"source_lang":"en","source_lang":"pt"}]}',
    b'{"version":1,"entries":[]} trailing',
    b'{"version":1,"namespaces":[]}',
])
def test_standalone_v1_is_closed(raw):
    with pytest.raises(CacheAdminError): list(iter_records(io.BytesIO(raw), limits()))


def test_cancel_in_empty_envelope_whitespace_batches():
    class Cancel:
        calls = 0
        def checkpoint(self, point):
            assert point == 'cache_between_entries'
            self.calls += 1
            return True if self.calls == 2 else None
    token = Cancel()
    raw = b'{"version":2,"namespaces":[]}' + b' ' * 200_000
    with pytest.raises(CacheAdminError, match='cancelled'):
        list(iter_records(io.BytesIO(raw), limits(), SimpleNamespace(cancellation=token)))
    assert token.calls == 2


def test_real_gib_input_boundary_streams_without_materializing():
    prefix = b'{"version":2,"namespaces":[]}'
    class WhitespaceStream:
        def __init__(self, size):
            self.remaining = size - len(prefix)
            self.prefix = prefix
            self.largest = 0
            self.calls = 0
        def read(self, n):
            assert 0 < n <= 65536
            self.largest = max(self.largest, n)
            self.calls += 1
            if self.prefix:
                data, self.prefix = self.prefix, b''
                return data
            count = min(n, self.remaining)
            self.remaining -= count
            return b' ' * count
    for size in (1024**3, 1024**3 + 1):
        stream = WhitespaceStream(size)
        if size == 1024**3:
            assert list(iter_records(stream, limits())) == []
        else:
            with pytest.raises(CacheAdminError, match='file_too_large'):
                list(iter_records(stream, limits()))
        assert stream.remaining == 0 and stream.largest == 65536 and stream.calls > 16000


def test_real_gib_output_boundary_streams_without_materializing(v2row):
    from ayvu.cache_identity import exact_entry_key
    from ayvu.cache_repository import _entry_digest
    desc, initial = v2row
    base = entry()
    max_value = 1024**2
    header = b'{"version":2,"namespaces":[{' + b','.join(
        json.dumps(k, separators=(',', ':')).encode() + b':' + json.dumps(desc[k], ensure_ascii=False, separators=(',', ':')).encode()
        for k in ('kind', 'scope_kind', 'scope_id')) + b',"entries":['
    suffix = b']}]}'  # namespace close, then envelope close
    def make(index, size):
        source = f'{index:06d}'
        key = exact_entry_key(base.key.scope, base.key.value_kind, initial['semantic_fingerprint'], source)
        row = dict(initial, source_text=source, source_digest=key.source_content_digest, value='x' * size)
        row['entry_digest'] = _entry_digest(key, source, row['value'], row['canonical_identity'].encode(), row['metadata'].encode(), row['provenance'].encode(), row['created_at'], row['provenance_digest'])
        return row
    from ayvu.cache_schema import V2_COLUMNS
    fields = V2_COLUMNS[2:]
    sample = make(0, 0)
    encoded_len = len(json.dumps({k: sample[k] for k in fields}, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())
    class CountingSink:
        def __init__(self): self.total = self.largest = self.calls = 0
        def write(self, data):
            self.total += len(data)
            self.largest = max(self.largest, len(data))
            self.calls += 1
            return len(data)
    def run(target):
        # All index strings, digests, and metadata have constant lengths.
        available = target - len(header) - len(suffix)
        full, remainder = divmod(available + 1, encoded_len + max_value + 1)
        if remainder == 0:
            sizes = [max_value] * full
        else:
            assert remainder > encoded_len + 1
            sizes = [max_value] * full + [remainder - encoded_len - 1]
        sink = CountingSink()
        rows = (make(index, size) for index, size in enumerate(sizes))
        result = write_envelope(sink, [(desc, rows)], limits())
        return sink, result
    sink, count = run(1024**3)
    assert sink.total == 1024**3 and count > 1000 and sink.calls > count
    assert sink.largest <= max_value + encoded_len
    with pytest.raises(CacheAdminError, match='file_too_large'):
        run(1024**3 + 1)
