"""Transient unit execution through the shared rich linguistic stages.

Offline execution uses canonical local evidence only. Preview explicitly supplies
a real provider and private raw overlay; neither route accepts durable writes,
memory or replay.
"""
from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from ..content import TranslationUnit, TextParts, protect_special_terms, protected_content_is_intact
from ..kernel import LanguagePair
from ..kernel import JobFailure, JobFailureFamily
from ..providers import ProviderStatus, TranslationProviderRequest, execute
from ..knowledge.cache import CacheIdentityError, CacheLookupState, CacheMissReason, CacheOnlyRequest, ExactCacheLookup
from .document_contracts import TranslationExecutionError, _template, _expand
from .engine import UnitExecution, STATUS_CACHE, STATUS_MISSING
from .local_cache_plan import LocalCachePlanResolver, LocalCachePlanState
from .chunking import split_text
from .pipeline import LinguisticStages, LinguisticSource


class ExactLookup(Protocol):
    def lookup(self, key) -> ExactCacheLookup: ...


class _LocalExact:
    def __init__(self, reader, resolver, source, template):
        self.reader, self.resolver, self.source, self.template = reader, resolver, source, template
        self.resolution = None

    def get(self, text, language_pair):
        if self.resolver is None:
            return None
        resolution = self.resolver.resolve(self.source)
        self.resolution = resolution
        if resolution.state is LocalCachePlanState.AMBIGUOUS:
            return ExactCacheLookup(CacheLookupState.AMBIGUOUS, reason=CacheMissReason.AMBIGUOUS_CANDIDATES)
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
                 cache_only: bool = False, chunk_limit: int = 3000, checkpoint=lambda _: None,
                 on_hit=lambda unit, resolution: None, on_resolution=lambda resolution: None,
                 provider=None, exact_cache=None, map_execution=lambda unit, result: result, on_estimate=None):
        self.pair, self.glossary = language_pair, apply_glossary
        self.readable_original = readable_original
        self.reader, self.resolver = reader, resolver
        self.cache_only, self.chunk_limit, self.checkpoint = cache_only, chunk_limit, checkpoint
        self.on_hit = on_hit
        self.on_resolution = on_resolution
        self.provider, self.exact_cache, self.map_execution = provider, exact_cache, map_execution
        # A dry run's estimate (#380) sees each request the run would send: the chunks the stages would make,
        # once per text, since a run's own cache serves a text it already translated.
        self.on_estimate, self._estimated = on_estimate, set()

    def execute(self, unit: TranslationUnit, *, readable_original=None) -> UnitExecution:
        template, originals = _template(unit)
        cache = (_LocalExact(self.reader, self.resolver, unit.source_text, template) if self.exact_cache is None
                 else self.exact_cache.for_unit(unit, template))
        outcome = LinguisticStages(self.provider or _NoProvider(), cache, self.glossary, on_stage=self.checkpoint).run(
            template, self.pair, dry_run=self.provider is None,
            cache_only=CacheOnlyRequest.resolve_compatible() if self.cache_only else False,
            chunk_limit=self.chunk_limit)
        self.on_resolution(cache.resolution)
        core = TextParts.from_text(template).core
        if outcome.source is LinguisticSource.DRY_RUN and self.on_estimate is not None and core not in self._estimated:
            self._estimated.add(core)
            for chunk in split_text(protect_special_terms(core).text, limit=self.chunk_limit):
                self.on_estimate(chunk)
        missing = outcome.source is LinguisticSource.CACHE_ONLY_MISSING
        status = (STATUS_CACHE if outcome.source is LinguisticSource.EXACT_CACHE else
                  STATUS_MISSING if missing else "dry_run" if self.provider is None else "translated")
        original = (readable_original or self.readable_original)(template, originals)
        if outcome.source is LinguisticSource.EXACT_CACHE:
            self.on_hit(unit, cache.resolution)
        return self.map_execution(unit, UnitExecution(outcome.text, status, original, _expand(outcome.text, originals),
                             missing_text=original if missing else None, glossary_usage=outcome.glossary_usage))


class TransientProvider:
    """Check real provider results against the discovered plan, without a job."""

    def __init__(self, provider, plan, *, cancellation=None):
        self.provider, self.plan, self.cancellation = provider, plan, cancellation

    def translate(self, text, source, target):
        result = execute(self.provider, TranslationProviderRequest(str(uuid4()), text, source, target,
            route=self.plan.route, policy_ref=self.plan.policy_ref, cancellation=self.cancellation))
        if result.status is not ProviderStatus.SUCCEEDED:
            raise TranslationExecutionError(result.failure)
        if result.producer != self.plan.producer or result.route != self.plan.route:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "plan_invalidated"))
        return result.text
