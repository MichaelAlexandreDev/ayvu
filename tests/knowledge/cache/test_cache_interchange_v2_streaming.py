import io
import json
from types import SimpleNamespace

import pytest

from ayvu.knowledge.cache.admin import CacheAdminError
from ayvu.knowledge.cache.interchange import iter_records, write_envelope
from ayvu.knowledge.cache.schema import V2_COLUMNS
from tests.support.cache_repository_v2_support import entry

from tests.knowledge.cache.cache_interchange_v2_support import InstrumentedInput, InstrumentedOutput, legacy, limits, v2row


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
    from ayvu.knowledge.cache.entries import exact_entry_key
    from ayvu.knowledge.cache.repository import _entry_digest
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
    from ayvu.knowledge.cache.schema import V2_COLUMNS
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
