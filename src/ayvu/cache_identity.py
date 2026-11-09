"""Versioned identity and authorization contract for exact and compatible cache reuse.

The values in this module are correlators. Their default representations and every
validation error are deliberately bounded and omit identity values.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable

from .job import validate_job_identity
from .provenance import ProducerIdentity

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
MAX_CANONICAL_PAYLOAD_BYTES = CACHE_ENTRY_V2_LIMITS.canonical_identity
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


def _canonical_revision(revision: OpaqueSemanticRevision | None) -> dict[str, object]:
    if revision is None:
        return {"type": "absent"}
    return {
        "type": "opaque_semantic_revision",
        "owner": {"type": "string", "value": revision.owner},
        "namespace": {"type": "string", "value": revision.namespace},
        "version": {"type": "integer", "value": revision.version},
        "value": {"type": "opaque_scalar", "value": revision.value},
        "snapshot_reference": ({"type": "durable_snapshot_reference", "value": revision.snapshot_reference}
                               if revision.snapshot_reference is not None else {"type": "absent"}),
    }


def _canonical_producer(producer: ProducerIdentity) -> dict[str, object]:
    return {
        "type": "producer_identity",
        "provider": {"type": "string", "value": producer.provider},
        "model": {"type": "string", "value": producer.model},
        "adapter": {"type": "string", "value": producer.adapter},
        "pipeline": {"type": "string", "value": producer.pipeline},
    }


def _canonical_value(value: CanonicalValue) -> dict[str, object]:
    if value.kind == "decimal":
        serialized: str | bool | int = format(value.value, "f")
    else:
        serialized = value.value  # type: ignore[assignment]
    return {"type": value.kind, "value": serialized}


def _canonical_mapping(entries: tuple[tuple[str, CanonicalValue], ...]) -> dict[str, object]:
    return {
        "type": "mapping",
        "entries": [
            {"key": {"type": "string", "value": key}, "value": _canonical_value(value)}
            for key, value in sorted(entries, key=lambda item: item[0])
        ],
    }


def _validate_named_mapping(value: object, code: str) -> tuple[tuple[str, CanonicalValue], ...]:
    if type(value) is not tuple or len(value) > MAX_COLLECTION_ENTRIES:
        raise CacheIdentityError("collection_limit_exceeded")
    normalized: list[tuple[str, CanonicalValue]] = []
    seen: set[str] = set()
    for pair in value:
        if type(pair) is not tuple or len(pair) != 2:
            raise CacheIdentityError(code)
        if not isinstance(pair[0], str):
            raise CacheIdentityError(code)
        canonical_key = unicodedata.normalize("NFC", pair[0])
        _safe_scalar(canonical_key, code)
        normalized_key = canonical_key.lower().replace("-", "_")
        if (normalized_key in _CONTENT_BEARING_MAPPING_KEYS
                or any(part in normalized_key for part in _CONTENT_BEARING_KEY_PARTS)
                or normalized_key in {"context_payload", "context_content", "book_context"}):
            raise CacheIdentityError("invalid_semantic_mapping_key")
        if canonical_key in seen:
            raise CacheIdentityError("duplicate_canonical_key")
        seen.add(canonical_key)
        if type(pair[1]) is not CanonicalValue:
            raise CacheIdentityError(code)
        normalized.append((canonical_key, pair[1]))
    return tuple(sorted(normalized, key=lambda item: item[0]))


@dataclass(frozen=True, repr=False)
class SemanticOperationV2:
    """Closed projection of every effective input to the cached translation stage."""

    operation: str
    source_locale: str
    target_locale: str
    route_hops: tuple[SemanticRouteHop, ...]
    pipeline_revision: OpaqueSemanticRevision
    stage_versions: tuple[tuple[str, int], ...]
    prompt_strategy: str
    prompt_revision: OpaqueSemanticRevision | None
    context_strategy: str
    context_revision: OpaqueSemanticRevision | None
    terminology_revision: OpaqueSemanticRevision | None
    generation_parameters: tuple[tuple[str, CanonicalValue], ...]
    segmentation_revision: OpaqueSemanticRevision
    chunk_limit: int
    chunking_revision: OpaqueSemanticRevision
    protection_revision: OpaqueSemanticRevision
    normalization_revision: OpaqueSemanticRevision
    format_constraints: tuple[tuple[str, CanonicalValue], ...] = ()
    version: int = CACHE_IDENTITY_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != CACHE_IDENTITY_VERSION:
            raise CacheIdentityError("unsupported_identity_version")
        _slug(self.operation, "invalid_operation")
        for locale in (self.source_locale, self.target_locale):
            if not isinstance(locale, str) or not _LOCALE.fullmatch(locale):
                raise CacheIdentityError("invalid_locale")
        if type(self.route_hops) is not tuple or not self.route_hops or len(self.route_hops) > MAX_ROUTE_HOPS:
            raise CacheIdentityError("route_hop_limit_exceeded")
        if any(type(hop) is not SemanticRouteHop for hop in self.route_hops):
            raise CacheIdentityError("invalid_route_hop")
        chain = (self.route_hops[0].source_locale, *(hop.target_locale for hop in self.route_hops))
        if chain[0] != self.source_locale or chain[-1] != self.target_locale:
            raise CacheIdentityError("invalid_route_chain")
        if any(left.target_locale != right.source_locale for left, right in zip(self.route_hops, self.route_hops[1:])):
            raise CacheIdentityError("invalid_route_chain")
        required_revisions = (self.pipeline_revision, self.segmentation_revision, self.chunking_revision,
                              self.protection_revision, self.normalization_revision)
        if any(type(item) is not OpaqueSemanticRevision for item in required_revisions):
            raise CacheIdentityError("missing_semantic_revision")
        if (self.prompt_revision is not None and type(self.prompt_revision) is not OpaqueSemanticRevision
                or self.context_revision is not None and type(self.context_revision) is not OpaqueSemanticRevision
                or self.terminology_revision is not None and type(self.terminology_revision) is not OpaqueSemanticRevision):
            raise CacheIdentityError("invalid_semantic_revision")
        _slug(self.prompt_strategy, "invalid_prompt_strategy")
        _slug(self.context_strategy, "invalid_context_strategy")
        if (self.prompt_strategy == "none") != (self.prompt_revision is None):
            raise CacheIdentityError("invalid_prompt_presence")
        if (self.context_strategy == "none") != (self.context_revision is None):
            raise CacheIdentityError("invalid_context_presence")
        for revision in (self.prompt_revision, self.context_revision, self.terminology_revision,
                         *(hop.configuration_revision for hop in self.route_hops)):
            if revision is not None and revision.snapshot_reference is None:
                raise CacheIdentityError("missing_semantic_snapshot")
        if type(self.chunk_limit) is not int or not 1 <= self.chunk_limit <= MAX_GENERATION_INTEGER:
            raise CacheIdentityError("invalid_chunk_limit")
        if type(self.stage_versions) is not tuple or len(self.stage_versions) > MAX_COLLECTION_ENTRIES:
            raise CacheIdentityError("collection_limit_exceeded")
        stage_names: set[str] = set()
        ordered_stages: list[tuple[str, int]] = []
        for pair in self.stage_versions:
            if type(pair) is not tuple or len(pair) != 2:
                raise CacheIdentityError("invalid_stage_versions")
            name = _slug(pair[0], "invalid_stage_versions")
            version = pair[1]
            if type(version) is not int or not 1 <= version <= MAX_GENERATION_INTEGER or name in stage_names:
                raise CacheIdentityError("invalid_stage_versions")
            stage_names.add(name)
            ordered_stages.append((name, version))
        if len(stage_names) != len(self.stage_versions):
            raise CacheIdentityError("duplicate_canonical_key")
        object.__setattr__(self, "stage_versions", tuple(ordered_stages))
        object.__setattr__(self, "generation_parameters", _validate_named_mapping(self.generation_parameters,
                                                                                  "invalid_generation_parameters"))
        object.__setattr__(self, "format_constraints", _validate_named_mapping(self.format_constraints,
                                                                                "invalid_format_constraints"))

    def __repr__(self) -> str:
        return "SemanticOperationV2(<redacted>)"

    def canonical_payload(self) -> bytes:
        """Language-independent UTF-8 JSON with fixed fields and explicit type tags."""
        value = {
            "format": {"type": "string", "value": "ayvu.semantic_operation"},
            "version": {"type": "integer", "value": self.version},
            "operation": {"type": "string", "value": self.operation},
            "source_locale": {"type": "locale", "value": self.source_locale},
            "target_locale": {"type": "locale", "value": self.target_locale},
            "route_hops": {"type": "sequence", "items": [
                {
                    "type": "route_hop",
                    "source_locale": {"type": "locale", "value": hop.source_locale},
                    "target_locale": {"type": "locale", "value": hop.target_locale},
                    "producer": _canonical_producer(hop.producer),
                    "capability_fingerprint": {"type": "capability_fingerprint", "value": hop.capability_fingerprint},
                    "configuration_revision": _canonical_revision(hop.configuration_revision),
                }
                for hop in self.route_hops
            ]},
            "pipeline_revision": _canonical_revision(self.pipeline_revision),
            "stage_versions": {"type": "ordered_stage_versions", "items": [
                {"type": "stage_version", "stage": {"type": "string", "value": name},
                 "version": {"type": "integer", "value": version}}
                for name, version in self.stage_versions
            ]},
            "prompt_strategy": {"type": "string", "value": self.prompt_strategy},
            "prompt_revision": _canonical_revision(self.prompt_revision),
            "context_strategy": {"type": "string", "value": self.context_strategy},
            "context_revision": _canonical_revision(self.context_revision),
            "terminology_revision": _canonical_revision(self.terminology_revision),
            "generation_parameters": _canonical_mapping(self.generation_parameters),
            "segmentation_revision": _canonical_revision(self.segmentation_revision),
            "chunk_limit": {"type": "integer", "value": self.chunk_limit},
            "chunking_revision": _canonical_revision(self.chunking_revision),
            "protection_revision": _canonical_revision(self.protection_revision),
            "normalization_revision": _canonical_revision(self.normalization_revision),
            "format_constraints": _canonical_mapping(self.format_constraints),
        }
        payload = json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_CANONICAL_PAYLOAD_BYTES:
            raise CacheIdentityError("canonical_payload_limit_exceeded")
        return payload

    @property
    def fingerprint(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_payload()).hexdigest()}"

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, object]]) -> SemanticOperationV2:
        """Parse a closed field set. Pair input detects duplicate keys before dict creation."""
        required = {
            "operation", "source_locale", "target_locale", "route_hops", "pipeline_revision", "stage_versions",
            "prompt_strategy", "prompt_revision", "context_strategy", "context_revision", "terminology_revision",
            "generation_parameters", "segmentation_revision", "chunk_limit", "chunking_revision", "protection_revision",
            "normalization_revision", "format_constraints", "version",
        }
        found: dict[str, object] = {}
        for key, value in pairs:
            if not isinstance(key, str) or key not in required or key in found:
                raise CacheIdentityError("invalid_projection_fields")
            found[key] = value
        if set(found) != required:
            raise CacheIdentityError("invalid_projection_fields")
        try:
            return cls(**found)  # type: ignore[arg-type]
        except TypeError:
            raise CacheIdentityError("invalid_projection_fields") from None


class CacheScopeKind(StrEnum):
    PROJECT = "project"
    LOCAL_USER = "local_user"
    WORK = "work"
    LEGACY_COMPATIBILITY = "legacy_compatibility"


_CACHE_SCOPE_AUTHORIZATION_TOKEN = object()


@dataclass(frozen=True, repr=False)
class CacheScope:
    kind: CacheScopeKind
    scope_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not CacheScopeKind:
            raise CacheIdentityError("invalid_cache_scope")
        _safe_scalar(self.scope_id, "invalid_cache_scope")

    def __repr__(self) -> str:
        return f"CacheScope(kind={self.kind.value!r}, scope_id=<redacted>)"


@dataclass(frozen=True, repr=False, init=False)
class AuthorizedCacheScope:
    project_id: str = field(repr=False)
    scope: CacheScope
    explicitly_broader: bool = False

    def __init__(self, project_id: str, scope: CacheScope, explicitly_broader: bool = False, *,
                 _authorization_token: object | None = None) -> None:
        if _authorization_token is not _CACHE_SCOPE_AUTHORIZATION_TOKEN:
            raise CacheIdentityError("cache_scope_denied")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "explicitly_broader", explicitly_broader)
        try:
            validate_job_identity(self.project_id)
        except Exception:
            raise CacheIdentityError("invalid_cache_scope") from None
        if type(self.scope) is not CacheScope or type(self.explicitly_broader) is not bool:
            raise CacheIdentityError("invalid_cache_scope")
        if self.scope.kind is CacheScopeKind.LEGACY_COMPATIBILITY:
            raise CacheIdentityError("legacy_scope_isolated")
        if self.scope.kind is CacheScopeKind.PROJECT:
            if self.scope.scope_id != self.project_id or self.explicitly_broader:
                raise CacheIdentityError("cache_scope_denied")
        elif not self.explicitly_broader:
            raise CacheIdentityError("cache_scope_denied")

    def __repr__(self) -> str:
        return f"AuthorizedCacheScope(kind={self.scope.kind.value!r}, scope_id=<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheAuthorizationPolicy:
    project_id: str = field(repr=False)
    broader_scopes: tuple[CacheScope, ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_job_identity(self.project_id)
        except Exception:
            raise CacheIdentityError("invalid_cache_scope") from None
        if type(self.broader_scopes) is not tuple or len(self.broader_scopes) > MAX_COLLECTION_ENTRIES:
            raise CacheIdentityError("collection_limit_exceeded")
        if any(type(item) is not CacheScope or item.kind in (CacheScopeKind.PROJECT, CacheScopeKind.LEGACY_COMPATIBILITY)
               for item in self.broader_scopes) or len(set(self.broader_scopes)) != len(self.broader_scopes):
            raise CacheIdentityError("invalid_cache_scope_policy")

    def resolve(self, scope: CacheScope | None = None) -> AuthorizedCacheScope:
        if scope is None:
            scope = CacheScope(CacheScopeKind.PROJECT, self.project_id)
        if type(scope) is not CacheScope:
            raise CacheIdentityError("cache_scope_denied")
        if scope.kind is CacheScopeKind.PROJECT and scope.scope_id == self.project_id:
            return AuthorizedCacheScope(self.project_id, scope, _authorization_token=_CACHE_SCOPE_AUTHORIZATION_TOKEN)
        if scope in self.broader_scopes:
            return AuthorizedCacheScope(self.project_id, scope, explicitly_broader=True,
                                        _authorization_token=_CACHE_SCOPE_AUTHORIZATION_TOKEN)
        raise CacheIdentityError("cache_scope_denied")


def legacy_compatibility_scope() -> CacheScope:
    """One named v1 namespace. V2 authorization never returns it."""
    return CacheScope(CacheScopeKind.LEGACY_COMPATIBILITY, "legacy-v1")


@dataclass(frozen=True, repr=False)
class ExactEntryKey:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)
    source_content_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind:
            raise CacheIdentityError("invalid_exact_entry_key")
        if not _DIGEST.fullmatch(self.semantic_fingerprint) or not _DIGEST.fullmatch(self.source_content_digest):
            raise CacheIdentityError("invalid_exact_entry_key")

    def __repr__(self) -> str:
        return f"ExactEntryKey(kind={self.value_kind.value!r}, identity=<redacted>)"


def source_content_digest(text: str) -> str:
    if not isinstance(text, str):
        raise CacheIdentityError("invalid_source_content")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise CacheIdentityError("invalid_source_content") from None
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def exact_entry_key(scope: AuthorizedCacheScope, value_kind: CacheValueKind, semantic_fingerprint: str,
                    source_text: str) -> ExactEntryKey:
    return ExactEntryKey(scope, value_kind, semantic_fingerprint, source_content_digest(source_text))


class CacheLookupState(StrEnum):
    HIT = "hit"
    MISS = "miss"
    AMBIGUOUS = "ambiguous"
    CORRUPT = "corrupt"


class CacheMissReason(StrEnum):
    ENTRY_NOT_FOUND = "entry_not_found"
    CACHE_INELIGIBLE = "cache_ineligible"
    IDENTITY_MISMATCH = "identity_mismatch"
    SCOPE_DENIED = "scope_denied"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    DUPLICATE_EXACT_KEY = "duplicate_exact_key"
    LEGACY_NAMESPACE_ISOLATED = "legacy_namespace_isolated"


@dataclass(frozen=True, repr=False)
class ExactCacheLookup:
    state: CacheLookupState
    value: str | None = field(default=None, repr=False)
    reason: CacheMissReason | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CacheLookupState or (self.state is CacheLookupState.HIT) != (self.value is not None):
            raise CacheIdentityError("invalid_cache_lookup")
        if self.value is not None and not isinstance(self.value, str):
            raise CacheIdentityError("invalid_cache_lookup")
        if self.state is CacheLookupState.HIT and self.reason is not None:
            raise CacheIdentityError("invalid_cache_lookup")
        if self.state is not CacheLookupState.HIT and type(self.reason) is not CacheMissReason:
            raise CacheIdentityError("invalid_cache_lookup")

    def __repr__(self) -> str:
        return f"ExactCacheLookup(state={self.state.value!r}, reason={None if self.reason is None else self.reason.value!r})"


@dataclass(frozen=True, repr=False)
class CacheProvenanceMetadata:
    operation: str
    producer: ProducerIdentity
    pipeline_revision: OpaqueSemanticRevision

    def __post_init__(self) -> None:
        _slug(self.operation, "invalid_cache_provenance")
        if type(self.producer) is not ProducerIdentity or type(self.pipeline_revision) is not OpaqueSemanticRevision:
            raise CacheIdentityError("invalid_cache_provenance")

    def __repr__(self) -> str:
        return "CacheProvenanceMetadata(<redacted>)"


@dataclass(frozen=True, repr=False)
class ExactCacheEntry:
    key: ExactEntryKey
    value: str = field(repr=False)
    provenance: CacheProvenanceMetadata
    source_text: str | None = field(default=None, repr=False)
    canonical_identity: bytes | None = field(default=None, repr=False)
    metadata: bytes = field(default=b"{}", repr=False)

    def __post_init__(self) -> None:
        if type(self.key) is not ExactEntryKey or not isinstance(self.value, str) or type(self.provenance) is not CacheProvenanceMetadata:
            raise CacheIdentityError("invalid_cache_entry")
        if (self.source_text is not None and not isinstance(self.source_text, str)
                or self.canonical_identity is not None and not isinstance(self.canonical_identity, bytes)
                or not isinstance(self.metadata, bytes)):
            raise CacheIdentityError("invalid_cache_entry")

    def __repr__(self) -> str:
        return "ExactCacheEntry(<redacted>)"


class PutIfAbsentState(StrEnum):
    INSERTED = "inserted"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    NOT_CACHEABLE = "not_cacheable"


@dataclass(frozen=True, repr=False)
class PutIfAbsentResult:
    state: PutIfAbsentState

    def __post_init__(self) -> None:
        if type(self.state) is not PutIfAbsentState:
            raise CacheIdentityError("invalid_put_result")

    def __repr__(self) -> str:
        return f"PutIfAbsentResult(state={self.state.value!r})"


class CacheOnlyMode(StrEnum):
    PINNED = "pinned"
    RESOLVE_COMPATIBLE = "resolve_compatible"


@dataclass(frozen=True, repr=False)
class PinnedExecutionIdentityReference:
    """Opaque reference to an identity that the local durable boundary must resolve."""

    boundary_reference: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_scalar(self.boundary_reference, "invalid_pinned_identity")

    def __repr__(self) -> str:
        return "PinnedExecutionIdentityReference(<redacted>)"


@dataclass(frozen=True, repr=False)
class ResolvedCacheExecutionIdentity:
    """Identity metadata resolved from a local durable project/job/provenance boundary."""

    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint)):
            raise CacheIdentityError("invalid_resolved_identity")

    def __repr__(self) -> str:
        return f"ResolvedCacheExecutionIdentity(kind={self.value_kind.value!r}, identity=<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheOnlyRequest:
    mode: CacheOnlyMode
    pin: PinnedExecutionIdentityReference | None = None

    def __post_init__(self) -> None:
        if type(self.mode) is not CacheOnlyMode:
            raise CacheIdentityError("invalid_cache_only_mode")
        if (self.mode is CacheOnlyMode.PINNED) != (self.pin is not None):
            raise CacheIdentityError("invalid_cache_only_request")
        if self.pin is not None and type(self.pin) is not PinnedExecutionIdentityReference:
            raise CacheIdentityError("invalid_cache_only_request")

    @classmethod
    def pinned(cls, reference: PinnedExecutionIdentityReference) -> CacheOnlyRequest:
        return cls(CacheOnlyMode.PINNED, reference)

    @classmethod
    def resolve_compatible(cls) -> CacheOnlyRequest:
        return cls(CacheOnlyMode.RESOLVE_COMPATIBLE)

    def __repr__(self) -> str:
        return f"CacheOnlyRequest(mode={self.mode.value!r})"


class CacheIdentityResolutionState(StrEnum):
    MISS = "miss"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    CORRUPT = "corrupt"


@dataclass(frozen=True, repr=False)
class CacheIdentityResolution:
    state: CacheIdentityResolutionState
    key: ExactEntryKey | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CacheIdentityResolutionState:
            raise CacheIdentityError("invalid_identity_resolution")
        if (self.state is CacheIdentityResolutionState.RESOLVED) != (self.key is not None):
            raise CacheIdentityError("invalid_identity_resolution")
        if self.key is not None and type(self.key) is not ExactEntryKey:
            raise CacheIdentityError("invalid_identity_resolution")

    def __repr__(self) -> str:
        return f"CacheIdentityResolution(state={self.state.value!r})"


def resolve_compatible_identities(candidates: Iterable[ExactEntryKey], *, scope: AuthorizedCacheScope,
                                  value_kind: CacheValueKind, source_digest: str) -> CacheIdentityResolution:
    """Resolve only a bounded, same-scope local candidate set; never choose by order or age."""
    if type(scope) is not AuthorizedCacheScope or type(value_kind) is not CacheValueKind or not _DIGEST.fullmatch(source_digest):
        raise CacheIdentityError("invalid_identity_resolution")
    collected = tuple(candidates)
    if len(collected) > MAX_CACHE_ONLY_CANDIDATES:
        raise CacheIdentityError("cache_candidate_limit_exceeded")
    if any(type(candidate) is not ExactEntryKey for candidate in collected):
        raise CacheIdentityError("invalid_identity_resolution")
    if any(candidate.scope != scope for candidate in collected):
        raise CacheIdentityError("cache_scope_denied")
    if any(candidate.value_kind is not value_kind or candidate.source_content_digest != source_digest for candidate in collected):
        raise CacheIdentityError("invalid_compatible_candidate")
    identities = {(item.semantic_fingerprint, item.source_content_digest, item.value_kind, item.scope) for item in collected}
    if len(identities) != len(collected):
        return CacheIdentityResolution(CacheIdentityResolutionState.CORRUPT)
    if not collected:
        return CacheIdentityResolution(CacheIdentityResolutionState.MISS)
    if len(collected) > 1:
        return CacheIdentityResolution(CacheIdentityResolutionState.AMBIGUOUS)
    return CacheIdentityResolution(CacheIdentityResolutionState.RESOLVED, collected[0])


@dataclass(frozen=True, repr=False)
class CompatibleMemoryQuery:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)
    source_text: str = field(repr=False)
    max_candidates: int

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint) or not isinstance(self.source_text, str)
                or type(self.max_candidates) is not int or not 1 <= self.max_candidates <= MAX_COLLECTION_ENTRIES):
            raise CacheIdentityError("invalid_memory_query")

    def __repr__(self) -> str:
        return "CompatibleMemoryQuery(<redacted>)"


@dataclass(frozen=True, repr=False)
class CacheCompatibilityIdentity:
    scope: AuthorizedCacheScope
    value_kind: CacheValueKind
    semantic_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.scope) is not AuthorizedCacheScope or type(self.value_kind) is not CacheValueKind
                or not _DIGEST.fullmatch(self.semantic_fingerprint)):
            raise CacheIdentityError("invalid_memory_identity")

    def __repr__(self) -> str:
        return f"CacheCompatibilityIdentity(kind={self.value_kind.value!r}, identity=<redacted>)"


@dataclass(frozen=True, repr=False)
class CompatibleMemoryCandidate:
    original: str = field(repr=False)
    translated: str = field(repr=False)
    score: float
    applied: bool
    identity: CacheCompatibilityIdentity

    def __post_init__(self) -> None:
        if (not isinstance(self.original, str) or not isinstance(self.translated, str)
                or type(self.score) not in (int, float) or not math.isfinite(self.score) or not 0 <= self.score <= 1
                or type(self.applied) is not bool or type(self.identity) is not CacheCompatibilityIdentity):
            raise CacheIdentityError("invalid_memory_candidate")

    def __repr__(self) -> str:
        return f"CompatibleMemoryCandidate(score={self.score!r}, applied={self.applied})"


def candidate_matches_query(candidate: CompatibleMemoryCandidate, query: CompatibleMemoryQuery) -> bool:
    """Defense in depth for ports: no scope, kind or semantic mismatch may reach the pipeline."""
    return (type(candidate) is CompatibleMemoryCandidate and candidate.identity.scope == query.scope
            and candidate.identity.value_kind is query.value_kind
            and candidate.identity.semantic_fingerprint == query.semantic_fingerprint)


def semantic_operation_from_canonical(payload: bytes) -> SemanticOperationV2:
    """Decode the closed v2 projection and require its exact canonical representation."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def scalar(value):
        return value["value"]

    def revision(value):
        if value == {"type": "absent"}:
            return None
        snapshot = value["snapshot_reference"]
        return OpaqueSemanticRevision(scalar(value["owner"]), scalar(value["namespace"]),
                                      scalar(value["version"]), scalar(value["value"]),
                                      None if snapshot == {"type": "absent"} else scalar(snapshot))

    def producer(value):
        return ProducerIdentity(*(scalar(value[name]) for name in ("provider", "model", "adapter", "pipeline")))

    def mapping(value):
        return tuple((scalar(item["key"]), CanonicalValue(item["value"]["type"], scalar(item["value"])))
                     for item in value["entries"])

    try:
        if not isinstance(payload, bytes) or len(payload) > MAX_CANONICAL_PAYLOAD_BYTES:
            raise ValueError()
        obj = json.loads(payload.decode("utf-8"), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        hops = tuple(SemanticRouteHop(scalar(hop["source_locale"]), scalar(hop["target_locale"]),
                                      producer(hop["producer"]), scalar(hop["capability_fingerprint"]),
                                      revision(hop["configuration_revision"]))
                     for hop in obj["route_hops"]["items"])
        stages = tuple((scalar(item["stage"]), scalar(item["version"])) for item in obj["stage_versions"]["items"])
        operation = SemanticOperationV2(
            scalar(obj["operation"]), scalar(obj["source_locale"]), scalar(obj["target_locale"]), hops,
            revision(obj["pipeline_revision"]), stages, scalar(obj["prompt_strategy"]), revision(obj["prompt_revision"]),
            scalar(obj["context_strategy"]), revision(obj["context_revision"]), revision(obj["terminology_revision"]),
            mapping(obj["generation_parameters"]), revision(obj["segmentation_revision"]), scalar(obj["chunk_limit"]),
            revision(obj["chunking_revision"]), revision(obj["protection_revision"]), revision(obj["normalization_revision"]),
            mapping(obj["format_constraints"]), scalar(obj["version"]))
        if operation.canonical_payload() != payload:
            raise ValueError()
        return operation
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError, AttributeError):
        raise CacheIdentityError("invalid_canonical_identity") from None
