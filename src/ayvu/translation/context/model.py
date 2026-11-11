"""Política determinística de contexto v1 (#164); nenhuma operação de I/O.

As referências esperadas vêm dos donos dos snapshots, nunca do texto. A porta
de leitura recebe apenas esse manifesto validado e limites que só podem diminuir.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import re
from typing import Callable, Protocol

from ...content import TranslationUnit, source_digest
from ...projects.profiles import ResolvedProfileSnapshot, canonical
from ...providers import ContextFeature, ContextItem, Requirement, UnitReference, valid_reference
from ...providers import Capability, ProviderCapabilitySnapshot, SupportState, LimitSemantics, LimitUnit
from ...knowledge.cache import OpaqueSemanticRevision
from ...kernel import JobError, validate_job_identity, validate_utc, JobWarning


class ContextError(ValueError):
    """Somente códigos fechados atravessam a fronteira de falha."""
    def __init__(self, code: str):
        self.code = code if code in CONTEXT_FAILURES else 'context_failed'
        super().__init__(self.code)


CONTEXT_FAILURES = frozenset({'context_failed', 'invalid_context', 'context_limit', 'context_authorization',
    'context_cancelled', 'context_source_failed', 'context_integrity', 'context_required_missing',
    'context_required_limit', 'context_required_unsupported', 'context_preflight_required', 'context_capabilities',
    'context_egress_denied', 'context_egress_mismatch'})


class ContextKind(StrEnum):
    PROFILE = 'profile'
    CURRENT_UNIT = 'current_unit'
    NEIGHBOR = 'neighbor'
    TERMINOLOGY = 'terminology'
    PROTECTED_TERM = 'protected_term'


class ContextCategory(StrEnum):
    PROJECT_SETTINGS = 'project_settings'
    DOCUMENT_TEXT = 'document_text'
    TERMINOLOGY = 'terminology'


class ContextPurpose(StrEnum):
    TRANSLATION = 'translation'


class OmissionReason(StrEnum):
    MISSING = 'missing'
    LIMIT = 'limit'
    UNSUPPORTED = 'unsupported'
    NEIGHBOR_LIMIT = 'neighbor_limit'


_CATEGORIES = {
    ContextKind.PROFILE: ContextCategory.PROJECT_SETTINGS,
    ContextKind.CURRENT_UNIT: ContextCategory.DOCUMENT_TEXT,
    ContextKind.NEIGHBOR: ContextCategory.DOCUMENT_TEXT,
    ContextKind.TERMINOLOGY: ContextCategory.TERMINOLOGY,
    ContextKind.PROTECTED_TERM: ContextCategory.TERMINOLOGY,
}


@dataclass(frozen=True)
class ContextLimits:
    entries: int = 64
    entry_bytes: int = 4096
    total_bytes: int = 32768
    neighbors: int = 8

    def __post_init__(self):
        if any(type(v) is not int or not 0 <= v <= cap for v, cap in
               zip((self.entries, self.entry_bytes, self.total_bytes, self.neighbors), (64, 4096, 32768, 8))):
            raise ContextError('context_limit')


@dataclass(frozen=True)
class ContextSourceLimits:
    entries: int = 64
    entry_bytes: int = 4096
    total_bytes: int = 32768

    def __post_init__(self):
        ContextLimits(self.entries, self.entry_bytes, self.total_bytes)


class ContextPolicyVersion(StrEnum):
    V1 = 'bounded_context_v1'

    @property
    def limits(self) -> ContextLimits:
        return ContextLimits()

    @property
    def manifest_entries(self) -> int:
        # Uma sentinela N+1 permite à porta recusar o lote sem carregar texto.
        # Não aumenta a coleção de conteúdo retornado nem a seleção de 64.
        return self.limits.entries + 1

    @property
    def precedence(self) -> tuple[ContextKind, ...]:
        return (ContextKind.PROFILE, ContextKind.CURRENT_UNIT, ContextKind.TERMINOLOGY,
                ContextKind.PROTECTED_TERM, ContextKind.NEIGHBOR)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _digest(value: object) -> bool:
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def content_identity(project: str, text: str) -> str:
    # Mesmo encoding de canonical, mas sem materializar texto arbitrário.
    prefix = canonical(('ayvu.context_content.v1', project))
    digest = hashlib.sha256(prefix + b's' + str(utf8_size(text)).encode('ascii') + b':')
    for start in range(0, len(text), 1024):
        digest.update(text[start:start + 1024].encode('utf-8'))
    return digest.hexdigest()


def utf8_size(text: str) -> int:
    """Conta sem criar uma cópia UTF-8 potencialmente grande."""
    if type(text) is not str:
        raise ContextError('invalid_context')
    total = 0
    for char in text:
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            raise ContextError('invalid_context')
        total += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
    return total


@dataclass(frozen=True, repr=False)
class ContextEntryRevision:
    """Manifesto do dono: identidade de conteúdo independente da revisão opaca."""
    project_id: str
    reference: str
    revision: str
    content_identity: str
    kind: ContextKind
    requirement: Requirement
    position: tuple[int, ...] = ()
    purpose: ContextPurpose = ContextPurpose.TRANSLATION

    def __post_init__(self):
        try:
            validate_job_identity(self.project_id)
        except JobError:
            raise ContextError('invalid_context') from None
        if (not valid_reference(self.reference) or self.reference is None
                or not _digest(self.revision) or not _digest(self.content_identity)
                or type(self.kind) is not ContextKind or type(self.requirement) is not Requirement
                or type(self.purpose) is not ContextPurpose or type(self.position) is not tuple
                or len(self.position) > 16 or any(type(n) is not int or not 0 <= n < 2**63 for n in self.position)
                or (self.kind in (ContextKind.CURRENT_UNIT, ContextKind.NEIGHBOR) and not self.position)):
            raise ContextError('invalid_context')

    @property
    def category(self) -> ContextCategory:
        return _CATEGORIES[self.kind]


@dataclass(frozen=True, repr=False)
class ContextSnapshot:
    profile: ResolvedProfileSnapshot
    unit: UnitReference
    current: TranslationUnit
    entries: tuple[ContextEntryRevision, ...]

    def __post_init__(self):
        if (type(self.profile) is not ResolvedProfileSnapshot or type(self.unit) is not UnitReference
                or type(self.current) is not TranslationUnit or self.current.unit_id != self.unit.unit_id
                or type(self.entries) is not tuple or len(self.entries) > ContextPolicyVersion.V1.manifest_entries
                or any(type(e) is not ContextEntryRevision for e in self.entries)):
            raise ContextError('invalid_context')
        self.profile.__post_init__()
        self.unit.__post_init__()
        for entry in self.entries:
            entry.__post_init__()
        if self.current.source_digest != source_digest(self.profile.project.value, self.current.source_text):
            raise ContextError('context_integrity')
        if (len({e.reference for e in self.entries}) != len(self.entries)
                or any(e.project_id != self.profile.project.value for e in self.entries)):
            raise ContextError('context_integrity')


@dataclass(frozen=True, repr=False)
class ContextAuthorization:
    project_id: str
    operation_id: str
    purpose: ContextPurpose
    source_kinds: frozenset[ContextKind]
    categories: frozenset[ContextCategory]
    policy: ContextPolicyVersion
    profile_revision: str
    unit: UnitReference
    issued_at: datetime
    expires_at: datetime


def validate_authorization(snapshot: ContextSnapshot, authorization: object, operation_id: str,
                           policy: ContextPolicyVersion, now: datetime) -> None:
    try:
        validate_utc(now)
        if type(snapshot) is not ContextSnapshot or type(authorization) is not ContextAuthorization:
            raise ValueError
        snapshot.__post_init__()
        a = authorization
        validate_job_identity(operation_id)
        validate_utc(a.issued_at)
        validate_utc(a.expires_at)
        if (type(policy) is not ContextPolicyVersion or a.policy is not policy
                or a.project_id != snapshot.profile.project.value or a.operation_id != operation_id
                or a.purpose is not ContextPurpose.TRANSLATION or a.unit != snapshot.unit
                or a.profile_revision != snapshot.profile.reference.value
                or not a.issued_at <= now < a.expires_at
                or type(a.source_kinds) is not frozenset or any(type(k) is not ContextKind for k in a.source_kinds)
                or type(a.categories) is not frozenset or any(type(k) is not ContextCategory for k in a.categories)
                or any(e.kind not in a.source_kinds or e.category not in a.categories for e in snapshot.entries)):
            raise ValueError
    except (ValueError, TypeError, AttributeError, JobError):
        raise ContextError('context_authorization') from None


@dataclass(frozen=True, repr=False)
class ContextCandidate:
    source: ContextEntryRevision
    text: str


@dataclass(frozen=True, repr=False)
class ContextSourceRequest:
    snapshot: ContextSnapshot
    source_kinds: frozenset[ContextKind]
    limits: ContextSourceLimits

    def __post_init__(self):
        if (type(self.snapshot) is not ContextSnapshot or type(self.limits) is not ContextSourceLimits
                or type(self.source_kinds) is not frozenset
                or self.source_kinds != frozenset(e.kind for e in self.snapshot.entries)):
            raise ContextError('invalid_context')
        self.snapshot.__post_init__()
        self.limits.__post_init__()


@dataclass(frozen=True, repr=False)
class ContextSourceOversize:
    """Sem conteúdo; limites e contagens saturados, nunca valores sem limite."""
    reason: OmissionReason = OmissionReason.LIMIT


@dataclass(frozen=True, repr=False)
class ContextSourceResult:
    candidates: tuple[ContextCandidate, ...] = ()
    oversize: ContextSourceOversize | None = None


class ContextCandidateSource(Protocol):
    def read(self, request: ContextSourceRequest) -> ContextSourceResult: ...


@dataclass(frozen=True, repr=False)
class ContextCapabilityEvidence:
    snapshot: ProviderCapabilitySnapshot
    expires_at: datetime

    def limits(self, now: datetime, configuration_ref: str | None, local: ContextLimits) -> tuple[SupportState, ContextLimits, int | None]:
        try:
            self.snapshot.__post_init__()
            validate_utc(self.expires_at)
            if self.snapshot.configuration_ref != configuration_ref or self.expires_at <= self.snapshot.observed_at:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ContextError('context_capabilities') from None
        state = self.snapshot.state_of(Capability.CONTEXT)
        if not self.snapshot.observed_at <= now < self.expires_at or state in (SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT):
            raise ContextError('context_preflight_required')
        # #103 v1 mede limites em caracteres. O limite de bytes local continua
        # independente; não fingimos que um caractere Unicode equivale a um byte.
        chars = None
        for limit in self.snapshot.limits:
            if limit.name == 'context_characters':
                if limit.unit is not LimitUnit.CHARACTERS or limit.semantics is LimitSemantics.UNKNOWN:
                    raise ContextError('context_preflight_required')
                if limit.semantics is LimitSemantics.KNOWN:
                    if type(limit.value) is not int:
                        raise ContextError('context_capabilities')
                    chars = limit.value
        # Um teto em bytes <= teto de caracteres é uma redução conservadora;
        # garante também o contrato #103 sem presumir ASCII.
        effective = local if chars is None else ContextLimits(local.entries, min(local.entry_bytes, chars),
            min(local.total_bytes, chars), local.neighbors)
        return state, effective, chars


@dataclass(frozen=True, repr=False)
class ContextOmission:
    source: ContextEntryRevision
    reason: OmissionReason


@dataclass(frozen=True, repr=False)
class ContextDestination:
    provider_id: str
    configuration_ref: str | None
    destination_ref: str
    policy_revision: int

    def __post_init__(self):
        # Todas as identidades são opacas; não há URL nem seletor de adapter.
        if (any(v is None or not valid_reference(v) for v in (self.provider_id, self.destination_ref))
                or not valid_reference(self.configuration_ref)
                or type(self.policy_revision) is not int or self.policy_revision < 1):
            raise ContextError('invalid_context')


@dataclass(frozen=True, repr=False)
class ContextEgressDisclosure:
    destination: ContextDestination
    operation_id: str
    purpose: ContextPurpose
    categories: tuple[ContextCategory, ...]
    selected_entries: int
    utf8_bytes: int
    provider_operation: str = 'translate'

    def __post_init__(self):
        try:
            validate_job_identity(self.operation_id)
        except JobError:
            raise ContextError('invalid_context') from None
        if (type(self.destination) is not ContextDestination or self.purpose is not ContextPurpose.TRANSLATION
                or self.provider_operation != 'translate' or type(self.categories) is not tuple
                or any(type(c) is not ContextCategory for c in self.categories)
                or tuple(sorted(set(self.categories), key=lambda c: c.value)) != self.categories
                or type(self.selected_entries) is not int or not 0 <= self.selected_entries <= 64
                or type(self.utf8_bytes) is not int or not 0 <= self.utf8_bytes <= 32768):
            raise ContextError('invalid_context')

    def binding(self) -> str:
        d = self.destination
        return _hash(('ayvu.context_egress.v1', d.provider_id, d.configuration_ref, d.destination_ref,
            d.policy_revision, self.operation_id, self.provider_operation, self.purpose.value,
            tuple(c.value for c in self.categories), self.selected_entries, self.utf8_bytes))

    def diagnostic(self) -> dict:
        return {'operation': self.provider_operation, 'purpose': self.purpose.value,
                'categories': tuple(c.value for c in self.categories),
                'entries': self.selected_entries, 'utf8_bytes': self.utf8_bytes}


@dataclass(frozen=True, repr=False)
class ContextBuildRequest:
    snapshot: ContextSnapshot
    authorization: ContextAuthorization
    operation_id: str
    capabilities: ContextCapabilityEvidence
    destination: ContextDestination
    limits: ContextLimits = ContextLimits()
    source_limits: ContextSourceLimits = ContextSourceLimits()
    policy: ContextPolicyVersion = ContextPolicyVersion.V1


@dataclass(frozen=True, repr=False)
class TranslationContext:
    selected: tuple[ContextEntryRevision, ...]
    omitted: tuple[ContextOmission, ...]
    warnings: tuple[JobWarning, ...]
    limits: ContextLimits
    feature: ContextFeature | None
    semantic_projection: bytes
    disclosure: ContextEgressDisclosure
    policy: ContextPolicyVersion

    @property
    def semantic_revision(self) -> OpaqueSemanticRevision | None:
        if self.feature is None:
            return None
        value = hashlib.sha256(self.semantic_projection).hexdigest()
        return OpaqueSemanticRevision('ayvu', 'translation_context', 1, 'context-effect:' + value,
                                      'context_effect:' + value)

    def diagnostic(self) -> dict:
        return {'policy': self.policy.value, 'selected': len(self.selected), 'utf8_bytes': self.disclosure.utf8_bytes,
                'omissions': tuple((o.source.kind.value, o.reason.value) for o in self.omitted)}


def _checkpoint(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise ContextError('context_cancelled')


class TranslationContextBuilder:
    def __init__(self, source: ContextCandidateSource):
        self._source = source

    def __repr__(self):
        return 'TranslationContextBuilder()'

    def validate(self, request: ContextBuildRequest, now: datetime) -> None:
        if type(request) is not ContextBuildRequest:
            raise ContextError('invalid_context')
        validate_authorization(request.snapshot, request.authorization, request.operation_id, request.policy, now)
        if type(request.limits) is not ContextLimits or type(request.source_limits) is not ContextSourceLimits:
            raise ContextError('context_limit')
        request.limits.__post_init__()
        request.source_limits.__post_init__()
        if type(request.destination) is not ContextDestination or type(request.capabilities) is not ContextCapabilityEvidence:
            raise ContextError('invalid_context')
        request.destination.__post_init__()

    def build(self, request: ContextBuildRequest, *, now: datetime,
              cancel: Callable[[], bool] | None = None) -> TranslationContext:
        try:
            return self._build(request, now=now, cancel=cancel)
        except ContextError:
            raise
        except Exception:
            raise ContextError('context_failed') from None

    def _build(self, request: ContextBuildRequest, *, now: datetime,
               cancel: Callable[[], bool] | None) -> TranslationContext:
        self.validate(request, now)
        state, limits, chars = request.capabilities.limits(now, request.destination.configuration_ref, request.limits)
        _checkpoint(cancel)
        snapshot = request.snapshot
        order = request.policy.precedence
        entries = tuple(sorted(snapshot.entries, key=lambda e: (order.index(e.kind), e.position, e.reference, e.revision)))
        required = any(e.requirement is Requirement.REQUIRED for e in entries)
        if state is SupportState.UNSUPPORTED and required:
            raise ContextError('context_required_unsupported')
        # Nem sequer carrega conteúdo sem capacidade explícita. O manifesto já
        # permite comunicar a redução exata e mantém o pedido LibreTranslate.
        if state is SupportState.UNSUPPORTED:
            candidates = ()
            oversize = False
        else:
            try:
                result = self._source.read(ContextSourceRequest(snapshot, frozenset(e.kind for e in entries), request.source_limits))
            except ContextError:
                raise
            except Exception:
                raise ContextError('context_source_failed') from None
            if (type(result) is not ContextSourceResult or type(result.candidates) is not tuple
                    or len(result.candidates) > request.source_limits.entries
                    or (result.oversize is not None and (type(result.oversize) is not ContextSourceOversize or result.candidates))):
                raise ContextError('context_source_failed')
            candidates, oversize = result.candidates, result.oversize is not None
        _checkpoint(cancel)
        by_ref = {}
        expected = {e.reference: e for e in entries}
        total = 0
        for c in candidates:
            if (type(c) is not ContextCandidate or type(c.source) is not ContextEntryRevision
                    or c.source != expected.get(c.source.reference) or c.source.reference in by_ref):
                raise ContextError('context_integrity')
            size = utf8_size(c.text)
            if not c.text:
                raise ContextError('context_integrity')
            total += size
            if size > request.source_limits.entry_bytes or total > request.source_limits.total_bytes:
                raise ContextError('context_source_failed')
            if content_identity(c.source.project_id, c.text) != c.source.content_identity:
                raise ContextError('context_integrity')
            by_ref[c.source.reference] = c
        selected, omitted, payload = [], [], []
        total, characters, neighbors = 0, 0, 0
        for e in entries:
            _checkpoint(cancel)
            c = by_ref.get(e.reference)
            reason = None
            if state is SupportState.UNSUPPORTED:
                reason = OmissionReason.UNSUPPORTED
            elif oversize:
                reason = OmissionReason.LIMIT
            elif c is None:
                reason = OmissionReason.MISSING
            elif e.kind is ContextKind.NEIGHBOR and neighbors >= limits.neighbors:
                reason = OmissionReason.NEIGHBOR_LIMIT
            elif (len(selected) >= limits.entries or utf8_size(c.text) > limits.entry_bytes
                    or total + utf8_size(c.text) + bool(payload) > limits.total_bytes
                    or (chars is not None and characters + len(c.text) + bool(payload) > chars)):
                reason = OmissionReason.LIMIT
            if reason is not None:
                if e.requirement is Requirement.REQUIRED:
                    raise ContextError('context_required_missing' if reason is OmissionReason.MISSING else 'context_required_limit')
                omitted.append(ContextOmission(e, reason))
                continue
            selected.append(e)
            payload.append(c.text)
            total += utf8_size(c.text) + (len(payload) > 1)
            characters += len(c.text) + (len(payload) > 1)
            neighbors += e.kind is ContextKind.NEIGHBOR
        _checkpoint(cancel)
        # Um item #141, até 64 entradas inteiras, separadas por LF. Papéis e
        # posições vêm somente do manifesto. Nenhuma string vira instrução.
        feature = None if not payload else ContextFeature((ContextItem('translation_context', '\n'.join(payload)),),
            Requirement.REQUIRED if any(e.requirement is Requirement.REQUIRED for e in selected) else Requirement.ADVISORY)
        projection = canonical(('ayvu.translation_context', request.policy.value,
            (limits.entries, limits.entry_bytes, limits.total_bytes, limits.neighbors, chars), 'joined_lf_v1',
            tuple((e.reference, e.revision, e.content_identity, e.kind.value, e.requirement.value, e.position) for e in selected)))
        warnings = tuple(JobWarning('context_' + reason.value) for reason in OmissionReason
                         if any(o.reason is reason for o in omitted))
        disclosure = ContextEgressDisclosure(request.destination, request.operation_id, ContextPurpose.TRANSLATION,
            tuple(sorted({e.category for e in selected}, key=lambda c: c.value)), len(selected), total)
        _checkpoint(cancel)
        return TranslationContext(tuple(selected), tuple(omitted), warnings, limits, feature, projection, disclosure, request.policy)
