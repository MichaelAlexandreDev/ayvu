"""Offline unit execution through the shared rich linguistic stages.

Only canonical evidence can resolve an exact key. No provider, memory, replay or
write port is accepted here, including when every unit is a cache hit.
"""
from __future__ import annotations

from typing import Protocol

from ..content import TranslationUnit, TextParts, protected_content_is_intact
from ..kernel import LanguagePair
from ..knowledge.cache import CacheIdentityError, CacheLookupState, CacheOnlyRequest, ExactCacheLookup
from .document_contracts import _template, _expand
from .engine import UnitExecution, STATUS_CACHE, STATUS_MISSING
from .local_cache_plan import LocalCachePlanResolver, LocalCachePlanState
from .pipeline import LinguisticStages, LinguisticSource


class ExactLookup(Protocol):
    def lookup(self, key) -> ExactCacheLookup: ...


class _LocalExact:
    def __init__(self, reader, resolver, source, template):
        self.reader, self.resolver, self.source, self.template = reader, resolver, source, template

    def get(self, text, language_pair):
        if self.resolver is None:
            return None
        resolution = self.resolver.resolve(self.source)
        if resolution.state is not LocalCachePlanState.RESOLVED:
            return None
        result = self.reader.lookup(resolution.key)
        if result.state is CacheLookupState.CORRUPT:
            raise CacheIdentityError("cache_entry_corrupt")
        if result.state is CacheLookupState.HIT and not protected_content_is_intact(
                TextParts.from_text(self.template).core, result.value):
            raise CacheIdentityError("cache_entry_corrupt")
        return result

    def set(self, *args):
        raise AssertionError("transient_cache_write_forbidden")


class _NoProvider:
    def translate(self, *args):
        raise AssertionError("transient_provider_forbidden")


class TransientUnits:
    """The dry-run gate follows typed cache-only precedence inside LinguisticStages."""

    def __init__(self, language_pair: LanguagePair, apply_glossary, readable_original, *,
                 reader: ExactLookup | None = None, resolver: LocalCachePlanResolver | None = None,
                 cache_only: bool = False, chunk_limit: int = 3000, checkpoint=lambda _: None):
        self.pair, self.glossary = language_pair, apply_glossary
        self.readable_original = readable_original
        self.reader, self.resolver = reader, resolver
        self.cache_only, self.chunk_limit, self.checkpoint = cache_only, chunk_limit, checkpoint

    def execute(self, unit: TranslationUnit) -> UnitExecution:
        template, originals = _template(unit)
        cache = _LocalExact(self.reader, self.resolver, unit.source_text, template)
        outcome = LinguisticStages(_NoProvider(), cache, self.glossary, on_stage=self.checkpoint).run(
            template, self.pair, dry_run=True,
            cache_only=CacheOnlyRequest.resolve_compatible() if self.cache_only else False,
            chunk_limit=self.chunk_limit)
        missing = outcome.source is LinguisticSource.CACHE_ONLY_MISSING
        status = (STATUS_CACHE if outcome.source is LinguisticSource.EXACT_CACHE else
                  STATUS_MISSING if missing else "dry_run")
        original = self.readable_original(template, originals)
        return UnitExecution(outcome.text, status, original, _expand(outcome.text, originals),
                             missing_text=original if missing else None, glossary_usage=outcome.glossary_usage)
