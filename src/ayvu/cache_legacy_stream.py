"""Streaming reader for the permissive legacy cache interchange format.

Legacy inputs retain json.loads(bytes) BOM and UTF-16/32 detection; v2 remains UTF-8.
Object key digests are delegated to a caller-owned bounded staging store. No
unknown object, array, string value, or set of keys is retained in memory.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import re
from types import SimpleNamespace

from .cache_admin import (MAX_ENTRIES, MAX_FILE_BYTES, MAX_METADATA_LENGTH, MAX_STRUCTURE_DEPTH,
                          MAX_STRUCTURE_TOKENS, MAX_TEXT_LENGTH, CacheAdminError, _invalid, checkpoint)
from .cache_interchange import _Reader

_FIELDS = frozenset(('source_lang', 'target_lang', 'original_text_hash', 'original_text',
                     'translated_text', 'created_at'))
_TEXT = frozenset(('original_text', 'translated_text'))
_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')


class _LegacyTranscoder:
    """Match json.loads(bytes) encoding detection while bounding original bytes."""
    def __init__(self, stream, limit, request=None):
        self.stream, self.limit = stream, limit
        self.request = request
        self.total = 0
        self.pending = bytearray()
        self.decoder = None
        self.done = False

    def _read(self, size):
        data = self.stream.read(min(size, max(1, self.limit - self.total + 1)))
        if not isinstance(data, bytes): _invalid('malformed_json')
        self.total += len(data)
        if self.total > self.limit: _invalid('file_too_large')
        if data and self.request is not None:
            checkpoint(self.request, 'cache_between_entries')
        return data

    def read(self, size):
        if self.decoder is None:
            initial = bytearray()
            while len(initial) < 4:
                data = self._read(4 - len(initial))
                if not data: break
                initial.extend(data)
            try:
                self.decoder = codecs.getincrementaldecoder(json.detect_encoding(initial))('surrogatepass')
                self.pending.extend(self.decoder.decode(bytes(initial), final=False).encode('utf-8', 'surrogatepass'))
            except (UnicodeError, LookupError): _invalid('malformed_json')
        while len(self.pending) < size and not self.done:
            data = self._read(32768)
            try:
                self.pending.extend(self.decoder.decode(data, final=not data).encode('utf-8', 'surrogatepass'))
            except UnicodeError: _invalid('malformed_json')
            if not data: self.done = True
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


class _Parser:
    def __init__(self, stream, key_seen, object_closed, request, max_bytes, max_entries):
        limit = min(max_bytes, MAX_FILE_BYTES)
        self.r = _Reader(_LegacyTranscoder(stream, limit, request), SimpleNamespace(raw_input=2 * limit + 4))
        self.key_seen, self.object_closed, self.request = key_seen, object_closed, request
        self.entry_ceiling = min(max_entries, MAX_ENTRIES)
        self.next_id = self.tokens = 0
        self.version = None
        self.has_version = self.has_entries = False
        self.first_entry_error = None
        self.too_many = self.invalid_entries = False

    def token(self):
        self.tokens += 1
        if self.tokens > MAX_STRUCTURE_TOKENS:
            _invalid('structure_too_large')

    def punctuation(self, char):
        self.r.expect(char)
        if char in (44, 58): self.token()

    def start(self, char, depth):
        if depth > MAX_STRUCTURE_DEPTH: _invalid('structure_too_deep')
        self.punctuation(char)
        self.token()

    def string(self, capture_limit=0, *, key=False):
        self.r.expect(34)
        decoder = codecs.getincrementaldecoder('utf-8')('surrogatepass')
        digest = hashlib.sha256() if key else None
        captured = []
        size = 0
        pending = None
        def emit(text):
            nonlocal size, pending
            if pending is not None:
                if text and 0xdc00 <= ord(text[0]) <= 0xdfff:
                    text = chr(0x10000 + ((pending - 0xd800) << 10) + ord(text[0]) - 0xdc00) + text[1:]
                else:
                    text = chr(pending) + text
                pending = None
            if text and 0xd800 <= ord(text[-1]) <= 0xdbff:
                pending = ord(text[-1]); text = text[:-1]
            if not text: return
            if digest is not None: digest.update(text.encode('utf-8', 'surrogatepass'))
            if capture_limit:
                needed = max(0, capture_limit + 1 - size)
                if needed: captured.append(text[:needed])
            size += len(text)
        def decode(data, final=False):
            try: emit(decoder.decode(data, final=final))
            except UnicodeError: _invalid('malformed_json')
        while True:
            c = self.r.peek()
            if c is None: _invalid('malformed_json')
            start = self.r.pos
            match = _SPECIAL.search(self.r.buf, start)
            end = match.start() if match else len(self.r.buf)
            if end > start:
                decode(self.r.buf[start:end]); self.r.pos = end
            if not match: continue
            c = self.r.take()
            if c == 34:
                decode(b'', True)
                if pending is not None:
                    character = chr(pending); pending = None
                    if digest is not None: digest.update(character.encode('utf-8', 'surrogatepass'))
                    if capture_limit and size <= capture_limit: captured.append(character)
                    size += 1
                break
            if c < 32: _invalid('malformed_json')
            decode(b'', True)
            decoder = codecs.getincrementaldecoder('utf-8')('surrogatepass')
            escape = self.r.take()
            translations = {34:'"', 92:'\\', 47:'/', 98:'\b', 102:'\f', 110:'\n', 114:'\r', 116:'\t'}
            if escape in translations: emit(translations[escape])
            elif escape == 117:
                raw = bytes(self.r.take() or 0 for _ in range(4))
                if not re.fullmatch(rb'[0-9a-fA-F]{4}', raw): _invalid('malformed_json')
                emit(chr(int(raw, 16)))
            else: _invalid('malformed_json')
        return ''.join(captured), size, digest.hexdigest() if digest is not None else None

    def number(self):
        state = 'start'
        digits = 0
        floating = False
        sample = bytearray()
        while (c := self.r.peek()) is not None and c in b'-+0123456789.eE':
            self.r.take()
            if len(sample) < 32: sample.append(c)
            if state == 'start' and c == 45: state = 'sign'
            elif state in ('start', 'sign') and c == 48: state = 'zero'; digits += 1
            elif state in ('start', 'sign') and 49 <= c <= 57: state = 'integer'; digits += 1
            elif state == 'integer' and 48 <= c <= 57: digits += 1
            elif state in ('zero', 'integer') and c == 46: state = 'dot'; floating = True
            elif state == 'dot' and 48 <= c <= 57: state = 'fraction'
            elif state == 'fraction' and 48 <= c <= 57: pass
            elif state in ('zero', 'integer', 'fraction') and c in (69, 101): state = 'exponent'; floating = True
            elif state == 'exponent' and c in (43, 45): state = 'exponent_sign'
            elif state in ('exponent', 'exponent_sign') and 48 <= c <= 57: state = 'exponent_digits'
            elif state == 'exponent_digits' and 48 <= c <= 57: pass
            else: _invalid('malformed_json')
        if state not in ('zero', 'integer', 'fraction', 'exponent_digits') or (not floating and digits > 4300):
            _invalid('malformed_json')
        return bytes(sample) if digits < 32 and not floating else None

    def value(self, depth):
        self.r.ws(); c = self.r.peek()
        if c == 123: self.object(depth); return
        if c == 91: self.array(depth); return
        if c == 34: self.string(); return
        if c in (45, *range(48, 58)): self.number(); return
        for literal in (b'true', b'false', b'null'):
            if c == literal[0]:
                for byte in literal:
                    if self.r.take() != byte: _invalid('malformed_json')
                return
        _invalid('malformed_json')

    def array(self, depth):
        self.start(91, depth)
        first = True
        while True:
            self.r.ws()
            if self.r.peek() == 93: self.r.take(); return
            if not first: self.punctuation(44)
            first = False
            self.value(depth + 1)

    def object(self, depth, mode='unknown'):
        self.start(123, depth)
        self.next_id += 1; object_id = self.next_id
        first = True
        entry = {}
        while True:
            self.r.ws()
            if self.r.peek() == 125:
                self.r.take(); self.object_closed(object_id)
                if mode == 'entry':
                    try:
                        self.validate_entry(entry)
                    except CacheAdminError as error:
                        if self.first_entry_error is None: self.first_entry_error = error.code
                        return None
                return entry
            if not first: self.punctuation(44)
            first = False
            name, length, digest = self.string(32, key=True)
            self.key_seen(object_id, digest)
            if length > 32: name = None
            self.punctuation(58)
            if mode == 'root' and name == 'version':
                self.has_version = True
                self.r.ws()
                if self.r.peek() in (45, *range(48, 58)):
                    self.version = self.number()
                else:
                    self.value(depth + 1); self.version = None
            elif mode == 'root' and name == 'entries':
                # Entries are consumed by the root generator, not this generic object.
                raise AssertionError('root handled separately')
            elif mode == 'entry' and name in _FIELDS:
                self.r.ws()
                if self.r.peek() != 34:
                    self.value(depth + 1); continue
                limit = MAX_TEXT_LENGTH if name in _TEXT else MAX_METADATA_LENGTH
                value, length, _ = self.string(limit)
                entry[name] = value
            else:
                self.value(depth + 1)

    def validate_entry(self, entry):
        if set(entry) != _FIELDS: _invalid('malformed_entry')
        for name in ('source_lang', 'target_lang', 'original_text_hash', 'original_text', 'translated_text', 'created_at'):
            if len(entry[name]) > (MAX_TEXT_LENGTH if name in _TEXT else MAX_METADATA_LENGTH): _invalid('text_too_large')
        try:
            for value in entry.values(): value.encode('utf-8')
            digest = hashlib.sha256(entry['original_text'].encode('utf-8')).hexdigest()
        except UnicodeError: _invalid('malformed_entry')
        if digest != entry['original_text_hash']: _invalid('invalid_text_hash')

    def records(self):
        self.r.ws()
        if self.r.peek() != 123:
            self.value(1)
            self.r.ws()
            if self.r.peek() is not None: _invalid('malformed_json')
            _invalid('malformed_payload')
        self.start(123, 1)
        self.next_id += 1; object_id = self.next_id
        first = True
        while True:
            self.r.ws()
            if self.r.peek() == 125:
                self.r.take(); self.object_closed(object_id); break
            if not first: self.punctuation(44)
            first = False
            name, length, digest = self.string(32, key=True)
            self.key_seen(object_id, digest)
            if length > 32: name = None
            self.punctuation(58); self.r.ws()
            if name == 'version':
                self.has_version = True
                if self.r.peek() in (45, *range(48, 58)): self.version = self.number()
                else: self.value(2)
            elif name == 'entries':
                self.has_entries = True
                if self.r.peek() != 91:
                    self.value(2)
                    self.invalid_entries = True
                    continue
                self.start(91, 2)
                count = 0
                while True:
                    self.r.ws()
                    if self.r.peek() == 93: self.r.take(); break
                    if count: self.punctuation(44)
                    count += 1
                    if self.request is not None: checkpoint(self.request, 'cache_between_entries')
                    self.r.ws()
                    if count > self.entry_ceiling:
                        self.too_many = True
                        self.value(3)
                    elif self.r.peek() != 123:
                        self.value(3)
                        if self.first_entry_error is None: self.first_entry_error = 'malformed_entry'
                    else:
                        entry = self.object(3, 'entry')
                        if entry is not None: yield entry
            else: self.value(2)
        self.r.ws()
        if self.r.peek() is not None: _invalid('malformed_json')
        if not self.has_version or self.version != b'1': _invalid('unsupported_version')
        if not self.has_entries or self.invalid_entries: _invalid('malformed_payload')
        if self.too_many: _invalid('too_many_entries')
        if self.first_entry_error is not None: _invalid(self.first_entry_error)


def iter_legacy_entries(stream, key_seen, object_closed, request=None, max_bytes=MAX_FILE_BYTES, max_entries=MAX_ENTRIES):
    """Yield six-field rows, checking the complete input when the iterator is exhausted."""
    if type(max_bytes) is not int or max_bytes < 0: _invalid('invalid_limits')
    if type(max_entries) is not int or max_entries < 0: _invalid('invalid_limits')
    yield from _Parser(stream, key_seen, object_closed, request, max_bytes, max_entries).records()
