"""Orquestração de contexto e consumo de uma decisão #159 já resolvida.

Não concede rede nem escolhe provider. A composição injeta uma decisão que
deve coincidir com a divulgação; o transporte existente ainda aplica #159.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Protocol
from ...providers import capability_fingerprint
from ...providers import NetworkClass, ResolvedProviderNetworkDecision, network_refusal
from .model import (
    ContextBuildRequest, ContextError, ContextEgressDisclosure, TranslationContext, TranslationContextBuilder,
    _hash,
)
from ...kernel import validate_utc


@dataclass(frozen=True, repr=False)
class ResolvedContextEgressDecision:
    disclosure: ContextEgressDisclosure
    allowed: bool
    issued_at: datetime
    expires_at: datetime
    network: ResolvedProviderNetworkDecision | None = None


class ContextEgressGate(Protocol):
    def decision(self, disclosure: ContextEgressDisclosure) -> ResolvedContextEgressDecision: ...


def require_context_egress(decision: object, disclosure: ContextEgressDisclosure, now: datetime, *,
                           trusted_issuers: frozenset[str] = frozenset({'network_policy'})) -> None:
    if type(disclosure) is not ContextEgressDisclosure:
        raise ContextError('context_egress_mismatch')
    try:
        if type(decision) is not ResolvedContextEgressDecision:
            raise ValueError
        validate_utc(now)
        validate_utc(decision.issued_at)
        validate_utc(decision.expires_at)
        if (type(decision.allowed) is not bool or not decision.allowed
                or not decision.issued_at <= now < decision.expires_at):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ContextError('context_egress_denied') from None
    if decision.disclosure != disclosure:
        raise ContextError('context_egress_mismatch')
    try:
        if type(decision.network) is not ResolvedProviderNetworkDecision:
            raise ValueError
        decision.network.__post_init__()
        if decision.network.network_class in (NetworkClass.STRICT_OFFLINE, NetworkClass.UNCLASSIFIED):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ContextError('context_egress_denied') from None
    reason = network_refusal(decision.network, binding='sha256:' + disclosure.binding(),
        trusted_issuers=trusted_issuers, policy_revision=disclosure.destination.policy_revision, now=now)
    if reason is not None:
        raise ContextError('context_egress_denied')


class ContextExecution:
    """Um conjunto explícito de unidades autorizadas; ausência nunca expande seleção."""
    def __init__(self, builder: TranslationContextBuilder, requests: tuple[ContextBuildRequest, ...], gate: ContextEgressGate, *,
                 trusted_issuers: frozenset[str] = frozenset({'network_policy'})):
        if (type(requests) is not tuple or not requests or any(type(r) is not ContextBuildRequest for r in requests)
                or len({r.snapshot.unit.unit_id for r in requests}) != len(requests)):
            raise ContextError('invalid_context')
        self.builder, self.requests, self.gate = builder, requests, gate
        self._trusted_issuers = trusted_issuers
        self._prepared: dict[str, TranslationContext] = {}
        self._prepared_requests: dict[str, ContextBuildRequest] = {}

    def __repr__(self):
        return 'ContextExecution()'

    def validate(self, now: datetime):
        try:
            for request in self.requests:
                self.builder.validate(request, now)
        except ContextError:
            raise
        except Exception:
            raise ContextError('context_failed') from None
        first = self.requests[0]
        if any((r.snapshot.profile.reference, r.operation_id, r.destination, r.policy)
               != (first.snapshot.profile.reference, first.operation_id, first.destination, first.policy)
               for r in self.requests):
            raise ContextError('context_authorization')

    def prepare(self, now: datetime, cancel: Callable[[], bool] | None = None):
        self._prepared = {}
        self._prepared_requests = {}
        self.validate(now)  # Todas as autorizações antes da primeira porta.
        prepared = {}
        for request in self.requests:
            prepared[request.snapshot.unit.unit_id] = self.builder.build(request, now=now, cancel=cancel)
        self._prepared = prepared
        self._prepared_requests = {r.snapshot.unit.unit_id: r for r in self.requests}

    def authorize_prepared(self, now: datetime):
        if len(self._prepared) != len(self.requests):
            raise ContextError('invalid_context')
        for context in self._prepared.values():
            self.authorize(context, now)

    @property
    def job_revision(self) -> str:
        if len(self._prepared) != len(self.requests):
            raise ContextError('invalid_context')
        # Só a projeção efetiva; autorização e candidatos omitidos não alteram raw.
        return 'sha256:' + _hash(tuple((key, self._prepared[key].semantic_projection.hex())
                                      for key in sorted(self._prepared)))

    def for_unit(self, document, unit, now: datetime, plan) -> TranslationContext:
        request = next((r for r in self.requests if r.snapshot.unit.unit_id == unit.unit_id), None)
        if request is None:
            raise ContextError('context_authorization')
        self.builder.validate(request, now)
        request.capabilities.limits(now, request.destination.configuration_ref, request.limits)
        if capability_fingerprint(request.capabilities.snapshot) != plan.capabilities:
            raise ContextError('context_capabilities')
        if (request.snapshot.profile.project.value != document.project_id
                or request.snapshot.unit.document_id != document.document_id
                or request.snapshot.unit.revision_id != document.revision_id or request.snapshot.current != unit
                or request.snapshot.profile.target_language != plan.route.target):
            raise ContextError('context_integrity')
        result = self._prepared.get(unit.unit_id)
        if result is None:
            raise ContextError('invalid_context')
        if self._prepared_requests.get(unit.unit_id) != request:
            raise ContextError('context_integrity')
        effective = request.capabilities.limits(now, request.destination.configuration_ref, request.limits)[1]
        if (result.policy is not request.policy or result.limits != effective
                or result.disclosure.destination != request.destination
                or len(result.selected) + len(result.omitted) != len(request.snapshot.entries)
                or any(e not in request.snapshot.entries for e in result.selected)
                or any(o.source not in request.snapshot.entries for o in result.omitted)):
            raise ContextError('context_integrity')
        return result

    def authorize(self, context: TranslationContext, now: datetime):
        try:
            decision = self.gate.decision(context.disclosure)
        except Exception:
            raise ContextError('context_egress_denied') from None
        require_context_egress(decision, context.disclosure, now, trusted_issuers=self._trusted_issuers)


def plan_with_context(plan, context: TranslationContext):
    revision = context.semantic_revision
    if revision is None:
        return plan
    return replace(plan, context_strategy='bounded_context_v1', context_ref=revision.snapshot_reference,
                   context_revision=revision)
