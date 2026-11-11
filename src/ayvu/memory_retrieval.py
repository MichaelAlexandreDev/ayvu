"""Conservative retrieval policy shared by production and future application callers.

No storage, UI or dataset policy lives here. Cross-document values may be
reviewed, but cannot be auto-applied without the original structural evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Callable

from .translation_memory import similarity_score

from .approved_memory import (
    MemoryQuery, MemoryRevision, MemoryScope, MemorySource, MemoryState, MemoryValidationError,
    PipelinePin, PipelineStage, RetrievalLimits, RetrievalMode, ValueKind, search_key, identity,
)
from .approved_memory_service import ApprovedMemoryRepository
from .cache_identity import AuthorizedCacheScope, ExactEntryKey
from .content import ContentDocument, TranslationUnit
from .provenance import ConfigurationRef
from .translation_plan import ResolvedTranslationPlan
from .content import protected_content_is_intact, special_term_spans


class RetrievalSource(StrEnum):
    EXACT_CACHE = "exact_cache"
    APPROVED_MEMORY = "approved_memory"
    LEGACY_CACHE = "legacy_cache"
    PROVIDER = "provider"


@dataclass(frozen=True, repr=False)
class RetrievalDecision:
    source: RetrievalSource
    auto_applied: bool
    reason: str
    score: float | None = None
    scope: MemoryScope | AuthorizedCacheScope | None = None
    lifecycle: MemoryState = MemoryState.MACHINE_CANDIDATE
    entry_id: str | None = None
    revision_id: str | None = None
    provenance_id: str | None = None
    cache_identity: ExactEntryKey | None = None
    reviewed: bool = False
    revision_number: int | None = None

    def __post_init__(self) -> None:
        if (type(self.source) is not RetrievalSource or type(self.auto_applied) is not bool
                or type(self.reviewed) is not bool or type(self.lifecycle) is not MemoryState
                or self.reason not in {"pipeline_pin_mismatch", "structure_unproven", "approved_exact", "approved_fuzzy",
                    "review_required", "eligible_exact_cache", "explicit_legacy_compatibility", "no_eligible_reuse",
                    "legacy_review_required"}
                or (self.score is not None and (type(self.score) not in (int, float)
                    or not math.isfinite(self.score) or not 0 <= self.score <= 1))):
            raise MemoryValidationError("invalid_retrieval_decision")
        for reference in (self.entry_id, self.revision_id, self.provenance_id):
            if reference is not None:
                identity(reference)
        if (self.cache_identity is not None and type(self.cache_identity) is not ExactEntryKey
                or self.scope is not None and type(self.scope) not in (MemoryScope, AuthorizedCacheScope)):
            raise MemoryValidationError("invalid_retrieval_decision")
        if self.source is not RetrievalSource.APPROVED_MEMORY and (self.reviewed or self.lifecycle is not MemoryState.MACHINE_CANDIDATE):
            raise MemoryValidationError("unreviewed_source_required")

    @property
    def dataset_eligible(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"RetrievalDecision(source={self.source.value!r}, auto_applied={self.auto_applied}, reason={self.reason!r})"


@dataclass(frozen=True, repr=False)
class RetrievalPolicy:
    exact_auto_apply: bool = True
    fuzzy_auto_apply: bool = False
    suggest_threshold: float = 0.80
    apply_threshold: float = 0.98
    legacy_cache_enabled: bool = False
    limits: RetrievalLimits = RetrievalLimits()

    def __post_init__(self) -> None:
        if (any(type(x) is not bool for x in (self.exact_auto_apply, self.fuzzy_auto_apply, self.legacy_cache_enabled))
                or any(type(x) not in (int, float) or not math.isfinite(x)
                       for x in (self.suggest_threshold, self.apply_threshold))
                or not 0 < self.suggest_threshold <= self.apply_threshold <= 1
                or (self.fuzzy_auto_apply and self.apply_threshold < 0.98)
                or type(self.limits) is not RetrievalLimits):
            raise MemoryValidationError("invalid_retrieval_policy")


@dataclass(frozen=True, repr=False)
class MemoryMatch:
    source: str
    target: str
    source_snippet: str
    target_snippet: str
    decision: RetrievalDecision


def snippet(text: str, maximum: int = 512) -> str:
    if type(maximum) is not int or not 1 <= maximum <= 512:
        raise MemoryValidationError("invalid_snippet_limit")
    return text.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def similarity(left: str, right: str) -> float:
    """Pinned NFC/casefold/whitespace policy; RapidFuzz normalized Indel ratio [0,1]."""
    return similarity_score(search_key(left), search_key(right))


def plan_memory_pin(plan: ResolvedTranslationPlan) -> PipelinePin:
    def ref(kind, value):
        data = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return ConfigurationRef(kind, kind + ':' + hashlib.sha256(data.encode('ascii')).hexdigest())
    return PipelinePin(
        ref('pipeline', [asdict(plan.pipeline_revision), [(s.stage_id, s.version) for s in plan.stages]]),
        ref('profile', plan.semantic_fingerprint),
        ConfigurationRef('glossary', plan.glossary_ref) if plan.glossary_ref is not None else ref('glossary', None),
    )


def structural_compatibility(document: ContentDocument, unit: TranslationUnit,
                             revision: MemoryRevision) -> bool:
    source = revision.value.source
    if (source.project_id != document.project_id or source.document_id != document.document_id
            or source.document_revision != document.revision_id or unit.content.protected_parts):
        return False
    original = next((item for item in document.units if item.unit_id == source.unit_id), None)
    if (original is None or original.content.protected_parts or any(getattr(original.content, dimension) != getattr(unit.content, dimension)
                   for dimension in ('kind', 'constraints', 'extensions', 'source_locale', 'context'))
            or MemorySource.from_document(document, original.unit_id) != source):
        return False
    # The governed store has no portable structural signature. Require a source
    # whose structure we can actually verify in this loaded document.
    def terms(text):
        return tuple(text[s.start:s.end] for s in special_term_spans(text))
    target = revision.value.target.text
    placeholders = re.compile(r"\{\{[^{}\n]+\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}|%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[sd]")
    return (placeholders.findall(unit.source_text) == placeholders.findall(target)
            and terms(original.source_text) == terms(unit.source_text)
            and not re.search(r"</?[A-Za-z]|__AYVU_", source.canonical_text + target)
            and bool(target.strip()) and protected_content_is_intact(unit.source_text, target))


class MemoryRetrieval:
    def __init__(self, repository: ApprovedMemoryRepository, policy: RetrievalPolicy = RetrievalPolicy()) -> None:
        self._repository, self.policy = repository, policy

    def __repr__(self) -> str:
        return "MemoryRetrieval()"

    def query(self, query: MemoryQuery, *, compatible: Callable[[MemoryRevision], bool | None] = lambda _: None
              ) -> tuple[MemoryMatch, ...]:
        if type(query) is not MemoryQuery:
            raise MemoryValidationError("invalid_memory_query")
        # Never collect an unbounded iterator from a broken port.
        results, used = [], 0
        for index, revision in enumerate(self._repository.retrieve(query)):
            if index >= query.limits.candidates:
                raise MemoryValidationError("repository_candidate_limit")
            if type(revision) is not MemoryRevision:
                raise MemoryValidationError("invalid_retrieval_result")
            value = revision.value
            sb, tb = len(value.source.canonical_text.encode('utf-8')), len(value.target.text.encode('utf-8'))
            used += sb + tb
            if sb > query.limits.source_bytes or tb > query.limits.target_bytes or used > query.limits.aggregate_bytes:
                raise MemoryValidationError("repository_byte_limit")
            if (revision.state is not MemoryState.APPROVED or value.scope not in query.scopes
                    or value.languages != query.languages or value.target.kind is not ValueKind.LINGUISTIC_FINAL
                    or value.target.stage is not PipelineStage.LINGUISTIC_FINAL
                    or value.source.normalization_version != query.normalization_version
                    or value.source.search_version != query.search_version):
                continue
            exact = value.source.canonical_text == query.canonical
            if query.mode is RetrievalMode.EXACT and not exact:
                continue
            if query.mode is RetrievalMode.CONCORDANCE and search_key(query.canonical) not in value.source.search_key:
                continue
            score = 1.0 if exact else similarity(query.canonical, value.source.canonical_text)
            if query.mode is RetrievalMode.FUZZY and score < self.policy.suggest_threshold:
                continue
            pinned = value.target.pin == query.pin
            structure = compatible(revision)
            if structure is False:
                continue  # Proven incompatibility is excluded; unknown evidence is suggestion-only.
            allowed = (self.policy.exact_auto_apply if exact else
                       self.policy.fuzzy_auto_apply and score >= self.policy.apply_threshold)
            applied = pinned and structure is True and allowed and query.mode is not RetrievalMode.CONCORDANCE
            reason = ('pipeline_pin_mismatch' if not pinned else 'structure_unproven' if not structure else
                      'approved_exact' if applied and exact else 'approved_fuzzy' if applied else 'review_required')
            decision = RetrievalDecision(RetrievalSource.APPROVED_MEMORY, applied, reason, score, value.scope,
                MemoryState.APPROVED, revision.entry_id, revision.revision_id,
                revision.event.provenance_id, reviewed=pinned, revision_number=revision.revision_number)
            results.append(MemoryMatch(value.source.canonical_text, value.target.text,
                snippet(value.source.canonical_text, query.limits.snippet_bytes),
                snippet(value.target.text, query.limits.snippet_bytes), decision))
        results.sort(key=lambda m: (not m.decision.auto_applied,
            query.scopes.index(m.decision.scope), -m.decision.score, -m.decision.revision_number,
            m.decision.entry_id, m.decision.revision_id))
        return tuple(results)


class LegacyCacheCompatibility:
    """Explicit machine/unreviewed source over metadata-bearing v2 cache rows.

    v1 rows have no trustworthy stage/identity, so they are never opened here.
    """
    def __init__(self, repository, policy: RetrievalPolicy, *, apply_threshold: float = 0.95) -> None:
        if not policy.legacy_cache_enabled or not 0 < apply_threshold <= 1:
            raise MemoryValidationError("legacy_compatibility_not_enabled")
        self.repository, self.policy, self.apply_threshold = repository, policy, apply_threshold

    def lookup(self, query):
        from .cache_identity import (
            CacheCompatibilityIdentity, CompatibleMemoryCandidate, CompatibleMemoryQuery, TRANSLATION_STAGE_AGGREGATE,
        )
        if (type(query) is not CompatibleMemoryQuery or query.max_candidates > self.policy.limits.candidates
                or query.value_kind is not TRANSLATION_STAGE_AGGREGATE
                or len(query.source_text.encode('utf-8')) > self.policy.limits.source_bytes):
            raise MemoryValidationError("invalid_memory_query")
        rows = self.repository.legacy_candidates(query, self.policy.limits)
        ranked, used = [], 0
        for index, (source, target, key) in enumerate(rows):
            if index >= query.max_candidates:
                raise MemoryValidationError("repository_candidate_limit")
            used += len(source.encode('utf-8')) + len(target.encode('utf-8'))
            if (len(source.encode('utf-8')) > self.policy.limits.source_bytes
                    or len(target.encode('utf-8')) > self.policy.limits.target_bytes
                    or used > self.policy.limits.aggregate_bytes):
                raise MemoryValidationError("repository_byte_limit")
            if (key.scope != query.scope or key.value_kind != query.value_kind
                    or key.semantic_fingerprint != query.semantic_fingerprint):
                continue
            score = similarity(query.source_text, source)
            if score >= self.policy.suggest_threshold:
                ranked.append((score, key.source_content_digest, source, target))
        if not ranked:
            return None
        score, _, source, target = min(ranked, key=lambda row: (-row[0], row[1]))
        return CompatibleMemoryCandidate(source, target, score, score >= self.apply_threshold,
            CacheCompatibilityIdentity(query.scope, query.value_kind, query.semantic_fingerprint))
