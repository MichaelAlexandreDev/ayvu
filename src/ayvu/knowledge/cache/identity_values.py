"""Validated identity values, eligibility and shared cache limits."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from ...provenance import ProducerIdentity


CACHE_IDENTITY_VERSION = 2
MAX_ROUTE_HOPS = 8
MAX_COLLECTION_ENTRIES = 256
MAX_OPAQUE_SCALAR_BYTES = 1024
MAX_CACHE_ONLY_CANDIDATES = MAX_COLLECTION_ENTRIES
MAX_GENERATION_INTEGER = 2**63 - 1
MAX_GENERATION_DECIMAL = Decimal("1000000000")


@dataclass(frozen=True)
class CacheEntryV2Limits:
    canonical_identity: int = 64 * 1024
    source_text: int = 1024 * 1024
    value: int = 1024 * 1024
    metadata: int = 64 * 1024
    provenance: int = 64 * 1024
    timestamp: int = 27


CACHE_ENTRY_V2_LIMITS = CacheEntryV2Limits()


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LOCALE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*")
_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_BARE_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_CAPABILITY_FINGERPRINT = re.compile(r"capfp:v[1-9][0-9]*:[0-9a-f]{64}")
_SNAPSHOT_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}")
_CONTENT_BEARING_MAPPING_KEYS = frozenset({
    "prompt", "prompt_text", "system_prompt", "user_prompt", "context", "context_text", "book_context",
    "terminology", "terminology_text", "glossary", "glossary_text", "endpoint", "endpoint_url",
    "endpoint_label", "display_name", "profile", "profile_name", "policy", "policy_ref", "credential",
    "credential_ref", "secret", "secret_ref", "token", "access_token", "api_key",
})
_CONTENT_BEARING_KEY_PARTS = ("prompt", "terminology", "glossary", "endpoint", "display_name", "profile_name",
                             "policy_ref", "credential", "secret", "access_token", "api_key")


class CacheIdentityError(ValueError):
    """Bounded contract failure; ``code`` never contains a caller value."""

    def __init__(self, code: str) -> None:
        if not _SLUG.fullmatch(code):
            code = "invalid_cache_identity"
        self.code = code
        super().__init__(code)


class CacheIdentityIneligible(CacheIdentityError):
    pass


class CacheIdentityConflict(CacheIdentityError):
    pass


@dataclass(frozen=True, repr=False)
class CacheValueKind:
    """Versioned value semantics; the production kind aggregates chunks and route hops."""

    name: str
    version: int

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.name) or type(self.version) is not int or not 1 <= self.version <= 2**31 - 1:
            raise CacheIdentityError("invalid_cache_value_kind")

    @property
    def value(self) -> str:
        return f"{self.name}:v{self.version}"

    def __repr__(self) -> str:
        return f"CacheValueKind({self.value!r})"


TRANSLATION_STAGE_AGGREGATE = CacheValueKind("translation_stage_aggregate", 1)
# Raw result for one unit after ordered chunks/route hops and protected-term restoration,
# before glossary processing.


class CacheEligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class CacheIneligibleReason(StrEnum):
    UNKNOWN_PROVIDER = "unknown_provider"
    UNKNOWN_MODEL = "unknown_model"
    UNKNOWN_ADAPTER = "unknown_adapter"
    UNKNOWN_PRODUCER_PIPELINE = "unknown_producer_pipeline"
    MISSING_CONFIGURATION_REVISION = "missing_configuration_revision"
    MISSING_CONTEXT_REVISION = "missing_context_revision"
    MISSING_PROMPT_REVISION = "missing_prompt_revision"
    MISSING_TERMINOLOGY_REVISION = "missing_terminology_revision"
    MISSING_SEMANTIC_SNAPSHOT = "missing_semantic_snapshot"
    REVISION_SNAPSHOT_MISMATCH = "revision_snapshot_mismatch"
    INVALID_SEMANTIC_INPUT = "invalid_semantic_input"


@dataclass(frozen=True, repr=False)
class CacheEligibility:
    state: CacheEligibilityState
    reason: CacheIneligibleReason | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CacheEligibilityState:
            raise CacheIdentityError("invalid_cache_eligibility")
        if self.state is CacheEligibilityState.ELIGIBLE and self.reason is not None:
            raise CacheIdentityError("invalid_cache_eligibility")
        if self.state is CacheEligibilityState.INELIGIBLE and type(self.reason) is not CacheIneligibleReason:
            raise CacheIdentityError("invalid_cache_eligibility")

    def __repr__(self) -> str:
        return f"CacheEligibility(state={self.state.value!r}, reason={None if self.reason is None else self.reason.value!r})"


def _safe_scalar(value: object, code: str = "invalid_opaque_scalar") -> str:
    if not isinstance(value, str) or not value or unicodedata.normalize("NFC", value) != value:
        raise CacheIdentityError(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise CacheIdentityError(code) from None
    if len(encoded) > MAX_OPAQUE_SCALAR_BYTES or any(character.isspace() or unicodedata.category(character).startswith("C")
                                                     for character in value):
        raise CacheIdentityError(code)
    return value


def _slug(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise CacheIdentityError(code)
    return value


@dataclass(frozen=True, repr=False)
class OpaqueSemanticRevision:
    """Owner-issued revision backed by a durable snapshot, never a fallback content hash."""

    owner: str
    namespace: str
    version: int
    value: str = field(repr=False)
    snapshot_reference: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _slug(self.owner, "invalid_semantic_revision")
        _slug(self.namespace, "invalid_semantic_revision")
        if type(self.version) is not int or not 1 <= self.version <= MAX_GENERATION_INTEGER:
            raise CacheIdentityError("invalid_semantic_revision")
        value = _safe_scalar(self.value, "invalid_semantic_revision")
        if _BARE_HASH.fullmatch(value) or value.lower().startswith(("http:", "https:")):
            raise CacheIdentityError("invalid_semantic_revision")
        if (self.snapshot_reference is not None
                and (not isinstance(self.snapshot_reference, str) or not _SNAPSHOT_REFERENCE.fullmatch(self.snapshot_reference))):
            raise CacheIdentityError("invalid_semantic_snapshot")

    def __repr__(self) -> str:
        return f"OpaqueSemanticRevision(owner={self.owner!r}, namespace={self.namespace!r}, version={self.version})"


@dataclass(frozen=True, repr=False)
class CanonicalValue:
    """An explicitly tagged generation or format-constraint scalar."""

    kind: str
    value: str | bool | int | Decimal = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind == "string":
            _safe_scalar(self.value)
            if _BARE_HASH.fullmatch(self.value) or self.value.lower().startswith(("http:", "https:")):
                raise CacheIdentityError("invalid_typed_value")
        elif self.kind == "boolean":
            if type(self.value) is not bool:
                raise CacheIdentityError("invalid_typed_value")
        elif self.kind == "integer":
            if type(self.value) is not int or not -MAX_GENERATION_INTEGER <= self.value <= MAX_GENERATION_INTEGER:
                raise CacheIdentityError("invalid_typed_value")
        elif self.kind == "decimal":
            decimal_value = self.value
            if isinstance(decimal_value, str):
                try:
                    decimal_value = Decimal(decimal_value)
                except InvalidOperation:
                    raise CacheIdentityError("invalid_typed_value") from None
            if (not isinstance(decimal_value, Decimal) or not decimal_value.is_finite()
                    or abs(decimal_value) > MAX_GENERATION_DECIMAL or decimal_value.as_tuple().exponent < -18):
                raise CacheIdentityError("invalid_typed_value")
            if decimal_value == 0:
                decimal_value = Decimal(0)
            else:
                decimal_value = decimal_value.normalize()
            object.__setattr__(self, "value", decimal_value)
        else:
            raise CacheIdentityError("unsupported_typed_value")

    def __repr__(self) -> str:
        return f"CanonicalValue(kind={self.kind!r})"


@dataclass(frozen=True, repr=False)
class SemanticRouteHop:
    source_locale: str
    target_locale: str
    producer: ProducerIdentity
    capability_fingerprint: str
    configuration_revision: OpaqueSemanticRevision | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.source_locale, str) or not _LOCALE.fullmatch(self.source_locale)
                or not isinstance(self.target_locale, str) or not _LOCALE.fullmatch(self.target_locale)):
            raise CacheIdentityError("invalid_route_hop")
        if type(self.producer) is not ProducerIdentity:
            raise CacheIdentityError("invalid_route_hop")
        if not isinstance(self.capability_fingerprint, str) or not _CAPABILITY_FINGERPRINT.fullmatch(self.capability_fingerprint):
            raise CacheIdentityError("invalid_route_hop")
        if self.configuration_revision is not None and type(self.configuration_revision) is not OpaqueSemanticRevision:
            raise CacheIdentityError("invalid_route_hop")

    def __repr__(self) -> str:
        return "SemanticRouteHop(<redacted>)"
