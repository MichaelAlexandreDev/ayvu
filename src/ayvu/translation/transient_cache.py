"""Exact raw reuse private to one preview invocation, after authorized v2 reads.

The overlay has no persistent scope or write adapter. Its semantic keys use the
same plan and source as v2; values enter before the current glossary is applied.
"""
from __future__ import annotations

from ..content import TextParts, protected_content_is_intact
from ..knowledge.cache import (
    CACHE_ENTRY_V2_LIMITS, CacheIdentityError, CacheLookupState,
    TRANSLATION_STAGE_AGGREGATE, exact_entry_key,
)


class TransientCache:
    def __init__(self, plan, *, reader=None, scope=None):
        self.fingerprint = plan.semantic_fingerprint
        self.reader, self.scope = reader, scope
        self._raw: dict[tuple[str, str], str] = {}

    def __repr__(self):
        return "TransientCache()"

    def clear(self):
        self._raw.clear()

    def for_unit(self, unit, template):
        return _UnitCache(self, unit.source_text, template)


class _UnitCache:
    resolution = None

    def __init__(self, overlay, source, template):
        self.overlay, self.source, self.template = overlay, source, template

    def _identity(self):
        if self.overlay.fingerprint is None or len(self.source.encode("utf-8")) > CACHE_ENTRY_V2_LIMITS.source_text:
            return None
        return self.overlay.fingerprint, self.source

    def get(self, text, language_pair):
        overlay, identity = self.overlay, self._identity()
        if identity is None:
            return None
        value = None
        if overlay.reader is not None and overlay.scope is not None:
            key = exact_entry_key(overlay.scope, TRANSLATION_STAGE_AGGREGATE, identity[0], self.source)
            result = overlay.reader.lookup(key)
            if result.state is CacheLookupState.CORRUPT:
                raise CacheIdentityError("cache_entry_corrupt")
            if result.state is CacheLookupState.HIT:
                value = result.value
        if value is None:
            value = overlay._raw.get(identity)
        if value is not None and not protected_content_is_intact(TextParts.from_text(self.template).core, value):
            raise CacheIdentityError("cache_entry_corrupt")
        return value

    def set(self, text, language_pair, translated):
        identity = self._identity()
        if identity is not None and len(translated.encode("utf-8")) <= CACHE_ENTRY_V2_LIMITS.value:
            self.overlay._raw[identity] = translated
