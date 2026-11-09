"""Bounded streaming interchange for closed cache v2 envelopes.

Namespace descriptors precede their entries so rows can be processed as they arrive.
The three stored JSON blobs are strings on the wire, preserving their exact bytes.
"""
from __future__ import annotations

import json
import re

from .cache_admin import _invalid, checkpoint, validate_payload, MAX_FILE_BYTES, MAX_ENTRIES
from .cache_identity import (CacheAuthorizationPolicy, CacheScope, CacheScopeKind,
                             CacheValueKind, ExactEntryKey, CacheIdentityError,
                             CacheProvenanceMetadata, OpaqueSemanticRevision)
from .provenance import ProducerIdentity
from .cache_repository import _valid_row
from .cache_schema import V2_COLUMNS

_ENTRY_FIELDS = V2_COLUMNS[2:]
_LEGACY_FIELDS = ("source_lang", "target_lang", "original_text_hash", "original_text", "translated_text", "created_at")
_BLOBS = frozenset(("canonical_identity", "metadata", "provenance"))


def _limit(limits, name, default):
    return getattr(limits, name, default)


def _descriptor(value):
    if type(value) is not dict:
        _invalid("malformed_namespace")
    if value == {"kind": "legacy-v1"}:
        return
    if set(value) != {"kind", "scope_kind", "scope_id"} or value.get("kind") != "v2":
        _invalid("malformed_namespace")
    try:
        scope = CacheScope(CacheScopeKind(value["scope_kind"]), value["scope_id"])
        if scope.kind is CacheScopeKind.LEGACY_COMPATIBILITY:
            raise ValueError()
    except (ValueError, TypeError, KeyError, CacheIdentityError):
        _invalid("malformed_namespace")


def validate_v2_entry(descriptor, entry):
    _descriptor(descriptor)
    if descriptor.get("kind") != "v2" or type(entry) is not dict or set(entry) != set(_ENTRY_FIELDS):
        _invalid("malformed_entry")
    if any(not isinstance(entry[name], str) for name in _ENTRY_FIELDS):
        _invalid("malformed_entry")
    try:
        scope = CacheScope(CacheScopeKind(descriptor["scope_kind"]), descriptor["scope_id"])
        # This temporary policy only checks the recorded identity; it grants no destination access.
        project = scope.scope_id if scope.kind is CacheScopeKind.PROJECT else "00000000-0000-0000-0000-000000000001"
        policy = CacheAuthorizationPolicy(project, () if scope.kind is CacheScopeKind.PROJECT else (scope,))
        authorized = policy.resolve(scope)
        match = re.fullmatch(r"([a-z][a-z0-9_-]{0,63}):v([1-9][0-9]*)", entry["value_kind"])
        if match is None:
            raise ValueError()
        key = ExactEntryKey(authorized, CacheValueKind(match[1], int(match[2])),
                            entry["semantic_fingerprint"], entry["source_digest"])
        row = tuple(entry[name].encode("utf-8") if name in _BLOBS else entry[name] for name in _ENTRY_FIELDS)
        values = dict(zip(_ENTRY_FIELDS, row))
        payload = tuple(values[n] for n in ("source_text", "value", "canonical_identity", "metadata", "provenance", "created_at", "provenance_digest", "entry_digest"))
        if not _valid_row(key, payload):
            raise ValueError()
        def unique(pairs):
            result = {}
            for name, item in pairs:
                if name in result:
                    raise ValueError()
                result[name] = item
            return result
        provenance = json.loads(entry["provenance"], object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(provenance) is not dict or set(provenance) != {"operation", "producer", "pipeline_revision"}:
            raise ValueError()
        producer = provenance["producer"]
        revision = provenance["pipeline_revision"]
        if (type(producer) is not dict or set(producer) != {"provider", "model", "adapter", "pipeline"}
                or type(revision) is not dict or set(revision) != {"owner", "namespace", "version", "value", "snapshot_reference"}):
            raise ValueError()
        CacheProvenanceMetadata(provenance["operation"], ProducerIdentity(**producer),
                                OpaqueSemanticRevision(**revision))
        return (scope.kind.value, scope.scope_id, *row)
    except (ValueError, TypeError, UnicodeError, CacheIdentityError, OverflowError):
        _invalid("malformed_entry")


def _entry(descriptor, value, limits):
    if descriptor["kind"] == "v2":
        validate_v2_entry(descriptor, value)
        row_limits = getattr(limits, "row_limits", None)
        if row_limits is not None:
            for name in ("canonical_identity", "source_text", "value", "metadata", "provenance", "created_at"):
                attr = "timestamp" if name == "created_at" else name
                if len(value[name].encode("utf-8")) > getattr(row_limits, attr):
                    _invalid("text_too_large")
    else:
        if type(value) is not dict or set(value) != set(_LEGACY_FIELDS):
            _invalid("malformed_entry")
        try:
            payload = json.dumps({"version": 1, "entries": [value]}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, TypeError):
            _invalid("malformed_entry")
        validate_payload(payload)


class _Reader:
    def __init__(self, stream, limits, request=None):
        self.stream, self.limits = stream, limits
        self.request = request
        self.buf = b""
        self.pos = self.total = 0
        self.raw_ceiling = _limit(limits, "raw_input", 1024**3)

    def peek(self):
        if self.pos == len(self.buf):
            self.buf = self.stream.read(65536)
            if not isinstance(self.buf, bytes):
                _invalid("malformed_json")
            self.pos = 0
            self.total += len(self.buf)
            if self.total > self.raw_ceiling:
                _invalid("file_too_large")
            if self.buf and self.request is not None:
                checkpoint(self.request, "cache_between_entries")
        return self.buf[self.pos] if self.pos < len(self.buf) else None

    def take(self):
        value = self.peek()
        if value is not None:
            self.pos += 1
        return value

    def ws(self):
        while self.peek() in (32, 9, 10, 13):
            match = re.match(rb"[ \t\r\n]+", self.buf[self.pos:])
            self.pos += match.end()

    def expect(self, byte):
        self.ws()
        if self.take() != byte:
            _invalid("malformed_json")

    def value(self, base_depth):
        self.ws()
        result = bytearray()
        quoted = escaped = False
        depth = 0
        while True:
            c = self.peek()
            if c is None or (not quoted and depth == 0 and c in (44, 58, 93, 125, 32, 9, 10, 13)):
                break
            self.take()
            result.append(c)
            if len(result) > min(_limit(self.limits, "raw_input", 1024**3), 32 * 1024 * 1024):
                _invalid("text_too_large")
            if quoted:
                if escaped: escaped = False
                elif c == 92: escaped = True
                elif c == 34: quoted = False
            elif c == 34: quoted = True
            elif c in (123, 91):
                depth += 1
                if base_depth + depth > _limit(self.limits, "depth", 8):
                    _invalid("structure_too_deep")
            elif c in (125, 93):
                depth -= 1
                if depth < 0: _invalid("malformed_json")
        def unique(pairs):
            obj = {}
            for k, v in pairs:
                if k in obj: _invalid("malformed_json")
                obj[k] = v
            return obj
        try:
            return json.loads(result.decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=lambda _: _invalid("malformed_json"))
        except (ValueError, UnicodeError, RecursionError):
            _invalid("malformed_json")


def iter_records(stream, limits, request=None):
    if _limit(limits, "depth", 8) < 2: _invalid("structure_too_deep")
    r = _Reader(stream, limits, request)
    r.expect(123)
    if r.value(1) != "version": _invalid("malformed_payload")
    r.expect(58)
    version = r.value(1)
    if type(version) is not int or version not in (1, 2): _invalid("unsupported_version")
    if version == 1:
        r.raw_ceiling = min(r.raw_ceiling, MAX_FILE_BYTES)
        if r.total > r.raw_ceiling: _invalid("file_too_large")
        r.expect(44)
        if r.value(1) != "entries": _invalid("malformed_payload")
        r.expect(58); r.expect(91)
        descriptor = {"kind": "legacy-v1"}
        yield descriptor, None
        count = 0
        while True:
            r.ws()
            if r.peek() == 93:
                r.take(); break
            if count: r.expect(44)
            count += 1
            if count > min(_limit(limits, "entries", 5_000_000), MAX_ENTRIES):
                _invalid("too_many_entries")
            if request is not None: checkpoint(request, "cache_between_entries")
            entry = r.value(2)
            _entry(descriptor, entry, limits)
            yield descriptor, entry
        r.expect(125); r.ws()
        if r.peek() is not None: _invalid("malformed_json")
        return
    if _limit(limits, "depth", 8) < 4: _invalid("structure_too_deep")
    r.expect(44)
    if r.value(1) != "namespaces": _invalid("malformed_payload")
    r.expect(58); r.expect(91)
    count = namespaces = 0
    seen = set()
    while True:
        r.ws()
        if r.peek() == 93:
            r.take(); break
        if namespaces: r.expect(44)
        namespaces += 1
        if namespaces > _limit(limits, "namespaces", 10000): _invalid("too_many_namespaces")
        r.expect(123)
        descriptor = {}
        while True:
            name = r.value(3)
            if not isinstance(name, str) or name in descriptor: _invalid("malformed_namespace")
            r.expect(58)
            if name == "entries": break
            if name not in ("kind", "scope_kind", "scope_id"): _invalid("malformed_namespace")
            descriptor[name] = r.value(3)
            r.expect(44)
        _descriptor(descriptor)
        if descriptor["kind"] == "v2" and len(descriptor["scope_id"].encode("utf-8")) > _limit(limits, "scope_reference", 1024): _invalid("malformed_namespace")
        identity = tuple(sorted(descriptor.items()))
        if identity in seen: _invalid("duplicate_namespace")
        seen.add(identity)
        yield descriptor, None
        r.expect(91)
        first = True
        legacy_count = 0
        while True:
            r.ws()
            if r.peek() == 93: r.take(); break
            if not first: r.expect(44)
            first = False
            if _limit(limits, "depth", 8) < 5: _invalid("structure_too_deep")
            count += 1
            if descriptor["kind"] == "legacy-v1":
                legacy_count += 1
                if legacy_count > MAX_ENTRIES: _invalid("too_many_entries")
            if count > _limit(limits, "entries", 5_000_000): _invalid("too_many_entries")
            if request is not None: checkpoint(request, "cache_between_entries")
            entry = r.value(4)
            _entry(descriptor, entry, limits)
            yield descriptor, entry
        r.expect(125)
    r.expect(125); r.ws()
    if r.peek() is not None: _invalid("malformed_json")


def write_envelope(stream, sections, limits, request=None):
    if _limit(limits, "depth", 8) < 4: _invalid("structure_too_deep")
    total = count = namespaces = 0
    seen = set()
    def emit(data):
        nonlocal total
        total += len(data)
        if total > _limit(limits, "serialized_export", 1024**3): _invalid("file_too_large")
        written = stream.write(data)
        if written is not None and written != len(data):
            raise OSError("short write")
    def encode(value):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            _invalid("malformed_entry")
    emit(b'{"version":2,"namespaces":[')
    for descriptor, rows in sections:
        _descriptor(descriptor)
        if descriptor["kind"] == "v2" and len(descriptor["scope_id"].encode("utf-8")) > _limit(limits, "scope_reference", 1024): _invalid("malformed_namespace")
        identity = tuple(sorted(descriptor.items()))
        if identity in seen: _invalid("duplicate_namespace")
        seen.add(identity)
        namespaces += 1
        if namespaces > _limit(limits, "namespaces", 10000): _invalid("too_many_namespaces")
        if namespaces > 1: emit(b",")
        fields = ("kind",) if descriptor["kind"] == "legacy-v1" else ("kind", "scope_kind", "scope_id")
        emit(b"{" + b",".join(encode(k) + b":" + encode(descriptor[k]) for k in fields) + b',"entries":[')
        first = True
        legacy_count = 0
        for entry in rows:
            if _limit(limits, "depth", 8) < 5: _invalid("structure_too_deep")
            count += 1
            if descriptor["kind"] == "legacy-v1":
                legacy_count += 1
                if legacy_count > MAX_ENTRIES: _invalid("too_many_entries")
            if count > _limit(limits, "entries", 5_000_000): _invalid("too_many_entries")
            if request is not None: checkpoint(request, "cache_between_entries")
            _entry(descriptor, entry, limits)
            if not first: emit(b",")
            first = False
            fields = _ENTRY_FIELDS if descriptor["kind"] == "v2" else _LEGACY_FIELDS
            emit(encode({k: entry[k] for k in fields}))
        emit(b"]}")
    emit(b"]}")
    return count
