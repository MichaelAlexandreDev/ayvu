import io
import json
from types import SimpleNamespace

import pytest

from ayvu.knowledge.cache.admin import CacheAdminError
from ayvu.knowledge.cache.interchange import iter_records, validate_v2_entry, write_envelope
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.cache.schema import V2_COLUMNS, participating_connection
from tests.support.cache_repository_v2_support import entry

from tests.knowledge.cache.cache_interchange_v2_support import InstrumentedInput, legacy, limits, v2row


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


def test_large_canonical_identity_and_maximum_provenance_together(tmp_path):
    from dataclasses import replace
    from ayvu.knowledge.cache import CanonicalValue, CACHE_ENTRY_V2_LIMITS, exact_entry_key
    from ayvu.knowledge.cache.repository import _digest, _entry_digest
    from tests.support.translation_cache_v2_support import make_plan
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
    from ayvu.knowledge.cache import CACHE_ENTRY_V2_LIMITS, exact_entry_key
    from ayvu.knowledge.cache.repository import _digest, _entry_digest
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
    from ayvu.knowledge.cache.identity_values import CACHE_ENTRY_V2_LIMITS
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
    from ayvu.knowledge.cache.repository import _digest, _entry_digest
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
