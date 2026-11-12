"""Canonical semantic projection and decoder for versioned cache identities."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from ...provenance import ProducerIdentity
from .identity_values import (
    CACHE_ENTRY_V2_LIMITS, CACHE_IDENTITY_VERSION, CacheIdentityError, CanonicalValue,
    MAX_COLLECTION_ENTRIES, MAX_GENERATION_INTEGER, MAX_ROUTE_HOPS, OpaqueSemanticRevision,
    SemanticRouteHop, _CONTENT_BEARING_KEY_PARTS, _CONTENT_BEARING_MAPPING_KEYS, _LOCALE,
    _safe_scalar, _slug,
)


MAX_CANONICAL_PAYLOAD_BYTES = CACHE_ENTRY_V2_LIMITS.canonical_identity


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
