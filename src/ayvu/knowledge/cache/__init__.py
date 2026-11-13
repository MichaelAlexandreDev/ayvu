"""The versioned exact cache: its identities, scopes and administration. The SQLite schema, repository,
interchange files and administration stores are adapter modules.
"""
from __future__ import annotations

from .admin import (
    CacheAdminError, CacheFilter, CacheNamespace, CacheRef, CacheScopeMapping, DiscardImport, ExecuteClean,
    ExecuteCleanRequest, ExecuteImport, ExecuteImportRequest, ExportCache, ExportReceipt, ExportRequest,
    FileRef, ImportCache, ImportCounts, ImportRequest, ImportScopeMap, InspectCache, InspectRequest,
    MAX_ENTRIES, MAX_FILE_BYTES, MAX_METADATA_LENGTH, MAX_STRUCTURE_DEPTH, MAX_STRUCTURE_TOKENS,
    MAX_TEXT_LENGTH, PreviewClean, PreviewCleanRequest, PreviewImport, _invalid, checkpoint, report,
    validate_payload,
)
from .scope import AuthorizedCacheScope, CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from .identity_values import (
    CACHE_ENTRY_V2_LIMITS, CACHE_IDENTITY_VERSION, CacheEligibility, CacheEligibilityState,
    CacheIdentityConflict, CacheIdentityError, CacheIneligibleReason, CacheValueKind, CanonicalValue,
    MAX_CACHE_ONLY_CANDIDATES, OpaqueSemanticRevision, SemanticRouteHop, TRANSLATION_STAGE_AGGREGATE,
)
from .compatible_memory import (
    CacheCompatibilityIdentity, CompatibleMemoryCandidate, CompatibleMemoryQuery, candidate_matches_query,
)
from .cache_only import (
    CacheIdentityResolutionState, CacheOnlyMode, CacheOnlyRequest, PinnedExecutionIdentityReference,
    ResolvedCacheExecutionIdentity, resolve_compatible_identities,
)
from .entries import (
    CacheLookupState, CacheMissReason, CacheProvenanceMetadata, CanonicalCacheRecord, ExactCacheEntry, ExactCacheLookup,
    ExactEntryKey, PutIfAbsentResult, PutIfAbsentState, exact_entry_key, source_content_digest,
)
from .identity import SemanticOperationV2, semantic_operation_from_canonical
from .legacy_schema import SCHEMA as LEGACY_SCHEMA

__all__ = [
    "AuthorizedCacheScope", "CACHE_ENTRY_V2_LIMITS", "CACHE_IDENTITY_VERSION", "CacheAdminError",
    "CacheAuthorizationPolicy", "CacheCompatibilityIdentity", "CacheEligibility", "CacheEligibilityState",
    "CacheFilter", "CacheIdentityConflict", "CacheIdentityError", "CacheIdentityResolutionState",
    "CacheIneligibleReason", "CacheLookupState", "CacheMissReason", "CacheNamespace", "CacheOnlyMode",
    "CacheOnlyRequest", "CacheProvenanceMetadata", "CacheRef", "CacheScope", "CacheScopeKind",
    "CacheScopeMapping", "CacheValueKind", "CanonicalCacheRecord", "CanonicalValue", "CompatibleMemoryCandidate",
    "CompatibleMemoryQuery", "DiscardImport", "ExactCacheEntry", "ExactCacheLookup", "ExactEntryKey",
    "ExecuteClean", "ExecuteCleanRequest", "ExecuteImport", "ExecuteImportRequest", "ExportCache",
    "ExportReceipt", "ExportRequest", "FileRef", "ImportCache", "ImportCounts", "ImportRequest",
    "ImportScopeMap", "InspectCache", "InspectRequest", "MAX_CACHE_ONLY_CANDIDATES", "MAX_ENTRIES",
    "MAX_FILE_BYTES", "MAX_METADATA_LENGTH", "MAX_STRUCTURE_DEPTH", "MAX_STRUCTURE_TOKENS", "MAX_TEXT_LENGTH",
    "OpaqueSemanticRevision", "PinnedExecutionIdentityReference", "PreviewClean", "PreviewCleanRequest",
    "PreviewImport", "PutIfAbsentResult", "PutIfAbsentState", "ResolvedCacheExecutionIdentity",
    "LEGACY_SCHEMA", "SemanticOperationV2", "SemanticRouteHop", "TRANSLATION_STAGE_AGGREGATE", "_invalid",
    "candidate_matches_query", "checkpoint", "exact_entry_key", "report", "resolve_compatible_identities",
    "semantic_operation_from_canonical", "source_content_digest", "validate_payload",
]
