"""The network decision a provider operation must present before any transport.

Nothing here evaluates policy or inspects a URL. A decision is an input made elsewhere, and this
only checks that it is well formed, trusted, current and bound to this exact operation. Saying
``unclassified`` is deliberate: it records that no policy was evaluated, instead of guessing a class.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .job import JobError, is_slug, validate_utc
from .provider import valid_reference

NETWORK_DECISION_VERSION = 1


class NetworkDecisionError(ValueError):
    """A bounded error: messages never include caller data."""


class NetworkClass(StrEnum):
    STRICT_OFFLINE = "strict_offline"
    LOOPBACK_ONLY = "loopback_only"
    LOCAL_NETWORK = "local_network"
    PRIVATE_REMOTE = "private_remote"
    PUBLIC_REMOTE = "public_remote"
    UNCLASSIFIED = "unclassified"


class ProviderOperation(StrEnum):
    AVAILABILITY = "availability"
    LANGUAGES = "languages"
    ROUTE = "route"
    TEST = "test"
    PREFLIGHT = "preflight"


def network_binding(instance_id: str, configuration_ref: str | None, operation: ProviderOperation) -> str:
    """Ties a decision to one provider, configuration and operation, so it cannot be replayed elsewhere."""
    text = json.dumps(["ayvu.provider_network", NETWORK_DECISION_VERSION, instance_id, configuration_ref, operation.value],
                      ensure_ascii=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('ascii')).hexdigest()}"


@dataclass(frozen=True, repr=False)
class ResolvedProviderNetworkDecision:
    allowed: bool
    network_class: NetworkClass
    reason_code: str
    binding: str
    policy_revision: int
    issuer: str
    issued_at: datetime
    expires_at: datetime
    policy_ref: str | None = None
    version: int = NETWORK_DECISION_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != NETWORK_DECISION_VERSION:
            raise NetworkDecisionError("unsupported_network_decision")
        if (type(self.allowed) is not bool or type(self.network_class) is not NetworkClass
                or not is_slug(self.reason_code) or not is_slug(self.issuer)
                or not isinstance(self.binding, str) or not self.binding.startswith("sha256:")
                or type(self.policy_revision) is not int or self.policy_revision < 0
                or not valid_reference(self.policy_ref)):
            raise NetworkDecisionError("malformed_network_decision")
        try:
            validate_utc(self.issued_at)
            validate_utc(self.expires_at)
        except JobError:
            raise NetworkDecisionError("malformed_network_decision") from None
        if self.expires_at <= self.issued_at:
            raise NetworkDecisionError("malformed_network_decision")

    def __repr__(self) -> str:
        return f"ResolvedProviderNetworkDecision(allowed={self.allowed}, reason_code={self.reason_code!r})"


def network_refusal(decision: object, *, binding: str, trusted_issuers: frozenset[str], policy_revision: int,
                    now: datetime) -> str | None:
    """Why this decision does not authorize this operation now, or None. Enforces; never evaluates."""
    if decision is None:
        return "network_decision_missing"
    if type(decision) is not ResolvedProviderNetworkDecision:
        return "network_decision_malformed"
    if decision.issuer not in trusted_issuers:
        return "network_decision_untrusted"
    if now < decision.issued_at:
        return "network_decision_not_yet_valid"
    if now >= decision.expires_at:
        return "network_decision_expired"
    if decision.binding != binding:
        return "network_decision_binding_mismatch"
    if decision.policy_revision != policy_revision:
        return "network_decision_revision_mismatch"
    if not decision.allowed:
        return "network_decision_denied"
    return None
