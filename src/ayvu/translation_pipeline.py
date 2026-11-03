"""The fixed linguistic sequence of one text, independent of any document format.

``LegacyTextPipelineFacade`` serves today's ``translate_text``. It keeps the current v1
cache semantics through a port the caller selects explicitly, and it has no durable
identity by design: it creates no artifact, provenance record or replay key.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from .chunking import split_text
from .domain import LanguagePair
from .translation_memory import TranslationMemoryMatch
from .translation_protection import (
    INTERNAL_MARKER_PATTERN, PROTECTED_TOKEN_PATTERN, TAG_TOKEN_PATTERN, ProtectedSpan, TextParts,
    protect_special_terms, protected_content_is_intact, special_term_spans,
)

DEFAULT_MAX_RECOVERY_CALLS = 64


@dataclass(frozen=True)
class TextStage:
    stage_id: str
    version: int = 1


# The order is the contract: a text that leaves early never reaches the stages after it.
LEGACY_TEXT_STAGES = (
    TextStage("unit_whitespace"),
    TextStage("exact_reuse"),
    TextStage("legacy_fuzzy_reuse"),
    TextStage("execution_gates"),
    TextStage("protection"),
    TextStage("provider_chunks"),
    TextStage("placeholder_restoration"),
    TextStage("raw_cache_write"),
    TextStage("post_processing"),
)


class LinguisticSource(StrEnum):
    BLANK = "blank"
    EXACT_CACHE = "exact_cache"
    APPLIED_MEMORY = "applied_memory"
    CACHE_ONLY_MISSING = "cache_only_missing"
    DRY_RUN = "dry_run"
    PROVIDER = "provider"


class TextProvider(Protocol):
    def translate(self, text: str, source: str, target: str) -> str: ...


class LegacyExactCache(Protocol):
    """Keys on exact text and language pair, as v1 does. A generic plan must not select this port."""

    def get(self, text: str, language_pair: LanguagePair) -> str | None: ...
    def set(self, text: str, language_pair: LanguagePair, translated: str) -> None: ...


class LegacyMemory(Protocol):
    def lookup(self, text: str, language_pair: LanguagePair) -> TranslationMemoryMatch | None: ...


class GlossaryOutcome(Protocol):
    text: str
    usage: object


@dataclass(frozen=True)
class LinguisticOutcome:
    text: str
    source: LinguisticSource
    glossary_usage: object | None = None
    memory_suggestion: TranslationMemoryMatch | None = None


def _lookup_memory_suggestion(memory: LegacyMemory | None, core: str,
                              language_pair: LanguagePair) -> TranslationMemoryMatch | None:
    """Find a fuzzy match for plain-text segments only.

    Segments carrying inline-tag placeholders are skipped, and matches whose
    stored text carries placeholders are rejected, so a reused translation can
    never inject another segment's markup. This keeps the memory conservative
    and limited to plain prose, where similar phrasing is most common.
    """
    if memory is None or TAG_TOKEN_PATTERN.search(core):
        return None
    match = memory.lookup(core, language_pair)
    if match is None:
        return None
    if TAG_TOKEN_PATTERN.search(match.original) or TAG_TOKEN_PATTERN.search(match.translated):
        return None
    if not protected_content_is_intact(core, match.translated):
        return None
    return match


def _translate_around_protected_terms(text: str, provider: TextProvider, source: str, target: str, chunk_limit: int,
                                      max_recovery_calls: int) -> str:
    """Retry with plain fragments; protected content never reaches the provider.

    This bounded fallback sacrifices some sentence context to preserve technical
    terms and inline markup when the provider cannot preserve our tokens.
    """
    output: list[str] = []
    cursor = 0
    recovery_calls = 0
    for span in [*special_term_spans(text), ProtectedSpan(len(text), len(text))]:
        parts = TextParts.from_text(text[cursor:span.start])
        if parts.core:
            fragments: list[str] = []
            offset = 0
            for chunk in split_text(parts.core, limit=chunk_limit):
                start = parts.core.index(chunk, offset)
                fragments.append(parts.core[offset:start])
                chunk_parts = TextParts.from_text(chunk)
                if not chunk_parts.core:
                    fragments.append(chunk)
                    offset = start + len(chunk)
                    continue
                if recovery_calls >= max_recovery_calls:
                    raise ValueError(
                        "A recuperação excedeu o limite de chamadas ao tradutor."
                    )
                recovery_calls += 1
                result = provider.translate(chunk_parts.core, source, target)
                if not result.strip():
                    raise ValueError("O tradutor devolveu texto vazio durante a recuperação.")
                fragments.append(chunk_parts.restore(result.strip()))
                offset = start + len(chunk)
            fragments.append(parts.core[offset:])
            translated = "".join(fragments)
            if INTERNAL_MARKER_PATTERN.search(translated):
                raise ValueError("O tradutor devolveu marcadores internos durante a recuperação.")
            output.append(parts.restore(translated))
        else:
            output.append(text[cursor:span.start])
        output.append(text[span.start:span.end])
        cursor = span.end
    return "".join(output)


class LegacyTextPipelineFacade:
    """Runs the fixed stages for one call. It holds no document, project or job identity."""

    def __init__(self, provider: TextProvider, cache: LegacyExactCache, apply_glossary: Callable[[str], GlossaryOutcome],
                 *, memory: LegacyMemory | None = None, max_recovery_calls: int = DEFAULT_MAX_RECOVERY_CALLS,
                 on_stage: Callable[[str], None] | None = None) -> None:
        self._provider = provider
        self._cache = cache
        self._apply_glossary = apply_glossary
        self._memory = memory
        self._max_recovery_calls = max_recovery_calls
        self._on_stage = on_stage

    def __repr__(self) -> str:
        return "LegacyTextPipelineFacade()"

    def _enter(self, stage_id: str) -> None:
        if self._on_stage is not None:
            self._on_stage(stage_id)

    def run(self, text: str, language_pair: LanguagePair, *, dry_run: bool = False, cache_only: bool = False,
            chunk_limit: int = 3000) -> LinguisticOutcome:
        self._enter("unit_whitespace")
        parts = TextParts.from_text(text)
        if not parts.core:
            return LinguisticOutcome(text, LinguisticSource.BLANK)

        self._enter("exact_reuse")
        cached = self._cache.get(parts.core, language_pair)
        if cached is not None and protected_content_is_intact(parts.core, cached):
            return self._finish(parts, cached, LinguisticSource.EXACT_CACHE)

        self._enter("legacy_fuzzy_reuse")
        suggestion = _lookup_memory_suggestion(self._memory, parts.core, language_pair)
        if suggestion is not None and suggestion.applied:
            return self._finish(parts, suggestion.translated, LinguisticSource.APPLIED_MEMORY)

        self._enter("execution_gates")
        if cache_only:
            return LinguisticOutcome(text, LinguisticSource.CACHE_ONLY_MISSING, memory_suggestion=suggestion)
        if dry_run:
            return LinguisticOutcome(text, LinguisticSource.DRY_RUN, memory_suggestion=suggestion)

        translated = self._translate(parts.core, language_pair, chunk_limit)

        self._enter("raw_cache_write")
        self._cache.set(parts.core, language_pair, translated)
        return self._finish(parts, translated, LinguisticSource.PROVIDER, suggestion)

    def _translate(self, core: str, language_pair: LanguagePair, chunk_limit: int) -> str:
        source, target = language_pair.source, language_pair.target
        self._enter("protection")
        protected = protect_special_terms(core)
        chunks = split_text(protected.text, limit=chunk_limit)
        expected_tokens = [token for token, _ in protected.terms]
        # Small chunk limits can split a token. Never send those broken tokens.
        chunk_tokens = [
            token for chunk in chunks for token in PROTECTED_TOKEN_PATTERN.findall(chunk)
        ]
        tokens_fit = chunk_tokens == expected_tokens
        translated = None
        self._enter("provider_chunks")
        if tokens_fit:
            response = "".join(self._provider.translate(chunk, source, target) for chunk in chunks)
            if response.strip() and PROTECTED_TOKEN_PATTERN.findall(response) == expected_tokens:
                restored = protected.restore(response)
                if protected_content_is_intact(core, restored):
                    translated = restored
        if translated is None:
            translated = _translate_around_protected_terms(
                core, self._provider, source, target, chunk_limit, self._max_recovery_calls
            )
        self._enter("placeholder_restoration")
        if not protected_content_is_intact(core, translated):
            raise ValueError("O tradutor devolveu marcadores internos ou perdeu termos protegidos.")
        return translated

    def _finish(self, parts: TextParts, translated: str, source: LinguisticSource,
                suggestion: TranslationMemoryMatch | None = None) -> LinguisticOutcome:
        """Glossary runs on every reused or new text, and the outer whitespace comes back last."""
        self._enter("post_processing")
        application = self._apply_glossary(translated)
        return LinguisticOutcome(parts.restore(application.text), source, application.usage, suggestion)
