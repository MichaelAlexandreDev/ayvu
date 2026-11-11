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
from .identity import (
    AuthorizedCacheScope, CACHE_ENTRY_V2_LIMITS, CACHE_IDENTITY_VERSION, CacheAuthorizationPolicy,
    CacheCompatibilityIdentity, CacheEligibility, CacheEligibilityState, CacheIdentityConflict,
    CacheIdentityError, CacheIdentityResolutionState, CacheIneligibleReason, CacheLookupState,
    CacheMissReason, CacheOnlyMode, CacheOnlyRequest, CacheProvenanceMetadata, CacheScope, CacheScopeKind,
    CacheValueKind, CanonicalValue, CompatibleMemoryCandidate, CompatibleMemoryQuery, ExactCacheEntry,
    ExactCacheLookup, ExactEntryKey, MAX_CACHE_ONLY_CANDIDATES, OpaqueSemanticRevision,
    PinnedExecutionIdentityReference, PutIfAbsentResult, PutIfAbsentState, ResolvedCacheExecutionIdentity,
    SemanticOperationV2, SemanticRouteHop, TRANSLATION_STAGE_AGGREGATE, candidate_matches_query,
    exact_entry_key, resolve_compatible_identities,
)

__all__ = [
    "AuthorizedCacheScope", "CACHE_ENTRY_V2_LIMITS", "CACHE_IDENTITY_VERSION", "CacheAdminError",
    "CacheAuthorizationPolicy", "CacheCompatibilityIdentity", "CacheEligibility", "CacheEligibilityState",
    "CacheFilter", "CacheIdentityConflict", "CacheIdentityError", "CacheIdentityResolutionState",
    "CacheIneligibleReason", "CacheLookupState", "CacheMissReason", "CacheNamespace", "CacheOnlyMode",
    "CacheOnlyRequest", "CacheProvenanceMetadata", "CacheRef", "CacheScope", "CacheScopeKind",
    "CacheScopeMapping", "CacheValueKind", "CanonicalValue", "CompatibleMemoryCandidate",
    "CompatibleMemoryQuery", "DiscardImport", "ExactCacheEntry", "ExactCacheLookup", "ExactEntryKey",
    "ExecuteClean", "ExecuteCleanRequest", "ExecuteImport", "ExecuteImportRequest", "ExportCache",
    "ExportReceipt", "ExportRequest", "FileRef", "ImportCache", "ImportCounts", "ImportRequest",
    "ImportScopeMap", "InspectCache", "InspectRequest", "MAX_CACHE_ONLY_CANDIDATES", "MAX_ENTRIES",
    "MAX_FILE_BYTES", "MAX_METADATA_LENGTH", "MAX_STRUCTURE_DEPTH", "MAX_STRUCTURE_TOKENS", "MAX_TEXT_LENGTH",
    "OpaqueSemanticRevision", "PinnedExecutionIdentityReference", "PreviewClean", "PreviewCleanRequest",
    "PreviewImport", "PutIfAbsentResult", "PutIfAbsentState", "ResolvedCacheExecutionIdentity",
    "SemanticOperationV2", "SemanticRouteHop", "TRANSLATION_STAGE_AGGREGATE", "_invalid",
    "candidate_matches_query", "checkpoint", "exact_entry_key", "report", "resolve_compatible_identities",
    "validate_payload",
]
