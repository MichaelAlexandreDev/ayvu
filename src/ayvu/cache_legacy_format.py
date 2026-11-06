"""The existing JSON-v1 value contract, independent of application ports."""
from __future__ import annotations

import hashlib
import json


class LegacyFormatError(ValueError):
    """Only a bounded validation code, never input text."""


def validate(payload, *, file_bytes, entries_limit, text_length, metadata_length,
             structure_depth, structure_tokens, check=None):
    def invalid(code):
        raise LegacyFormatError(code)

    if not isinstance(payload, bytes): invalid("invalid_payload")
    if len(payload) > file_bytes: invalid("file_too_large")
    depth = tokens = 0
    quoted = escaped = False
    for byte in payload:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 34: quoted = True
        elif byte in (123, 91):
            depth += 1
            tokens += 1
            if depth > structure_depth: invalid("structure_too_deep")
        elif byte in (125, 93): depth -= 1
        elif byte in (44, 58): tokens += 1
        if tokens > structure_tokens: invalid("structure_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: invalid("malformed_json")
            result[key] = value
        return result
    try:
        data = json.loads(payload, object_pairs_hook=pairs,
                          parse_constant=lambda _: invalid("malformed_json"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, LegacyFormatError): raise
        invalid("malformed_json")
    if not isinstance(data, dict): invalid("malformed_payload")
    if type(data.get("version")) is not int or data["version"] != 1:
        invalid("unsupported_version")
    entries = data.get("entries")
    if not isinstance(entries, list): invalid("malformed_payload")
    if len(entries) > entries_limit: invalid("too_many_entries")
    fields = ("source_lang", "target_lang", "original_text_hash", "original_text", "translated_text", "created_at")
    for entry in entries:
        if check is not None: check()
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) for key in fields):
            invalid("malformed_entry")
        for key in fields:
            if len(entry[key]) > (text_length if key in ("original_text", "translated_text") else metadata_length):
                invalid("text_too_large")
        try:
            digest = hashlib.sha256(entry["original_text"].encode("utf-8")).hexdigest()
            for key in fields: entry[key].encode("utf-8")
        except UnicodeEncodeError: invalid("malformed_entry")
        if digest != entry["original_text_hash"]: invalid("invalid_text_hash")
    return len(entries)
