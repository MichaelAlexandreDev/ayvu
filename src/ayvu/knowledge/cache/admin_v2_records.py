"""Cache interchange scope mapping and verified record representation."""
from __future__ import annotations

import hashlib
import json
import os

from .admin_store import CacheStorageError
from .admin import namespace_reference
from .interchange import iter_records, validate_v2_entry


_LEGACY_FIELDS = ('source_lang', 'target_lang', 'original_text_hash', 'original_text', 'translated_text', 'created_at')


def _map(scope_map, limits):
    if scope_map is None or len(scope_map.entries) > limits.scope_map_entries:
        raise CacheStorageError('invalid_scope_map')
    result = {}
    for item in scope_map.entries:
        src, dst = namespace_reference(item.source), namespace_reference(item.destination)
        for value in (*src, *dst):
            if len(value.encode('utf-8')) > limits.scope_reference:
                raise CacheStorageError('scope_reference_limit')
        result[src] = dst
    encoded = json.dumps(sorted((list(src), list(dst)) for src, dst in result.items()), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > limits.scope_map_bytes:
        raise CacheStorageError('scope_map_limit')
    return result


def _remap(descriptor, entry, destination):
    if descriptor['kind'] == 'legacy-v1':
        return tuple(entry[k] for k in _LEGACY_FIELDS)
    row = list(validate_v2_entry(descriptor, entry))
    row[:2] = destination[1:]
    # Match cache_repository._entry_digest's length-prefixed key and payload.
    parts = (*row[:5], row[6], row[7], row[5], row[8], row[9], row[10], row[11])
    h = hashlib.sha256()
    for value in parts:
        data = value if isinstance(value, bytes) else value.encode('utf-8')
        h.update(len(data).to_bytes(8, 'big'))
        h.update(data)
    row[12] = 'sha256:' + h.hexdigest()
    return tuple(row)


def _row_json(row):
    return json.dumps([{'blob': v.hex()} if isinstance(v, bytes) else v for v in row], ensure_ascii=True, separators=(',', ':'))


def _read_row(value):
    return tuple(bytes.fromhex(v['blob']) if isinstance(v, dict) and set(v) == {'blob'} else v for v in json.loads(value))


def _verified_records(stream, request, expected_digest):
    """Bind the bytes actually parsed, even if an in-place edit is restored."""
    info = os.fstat(stream.fileno())
    digest = hashlib.sha256(f'{info.st_dev}:{info.st_ino}:'.encode('ascii'))
    class HashedInput:
        def read(self, size):
            data = stream.read(size)
            digest.update(data)
            return data
        def tell(self):
            return stream.tell()
    yield from iter_records(HashedInput(), request.limits, request)
    if digest.hexdigest() != expected_digest:
        raise CacheStorageError('stale_preview')
