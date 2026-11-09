"""Framework-independent administration of legacy and semantic cache namespaces.

References and payloads are deliberately excluded from representations. Ports own
transaction boundaries and atomic publication; services never open local files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from .cache_legacy_format import validate as _validate_legacy, LegacyFormatError
from .job_events import JobFailure, JobFailureFamily, JobWarning
from .cache_identity import AuthorizedCacheScope, CACHE_ENTRY_V2_LIMITS, CacheEntryV2Limits

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 1_000_000
MAX_TEXT_LENGTH = 1_000_000
MAX_METADATA_LENGTH = 256
MAX_STRUCTURE_TOKENS = MAX_ENTRIES * 20 + 10
MAX_STRUCTURE_DEPTH = 16
SAFE_POINTS = ("cache_before_read", "cache_between_entries", "cache_before_mutation",
               "cache_before_commit", "cache_before_publish")


class CacheAdminError(Exception):
    """Bounded error code from a trusted adapter, never an exception message."""
    def __init__(self, code: str, family: JobFailureFamily = JobFailureFamily.RESOURCE):
        if code != "v2-admin-required" and (not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)):
            code = "unexpected_error"
        self.code, self.family = code, family
        super().__init__(code)


def _invalid(code: str) -> None:
    raise CacheAdminError(code, JobFailureFamily.INPUT) from None


@dataclass(frozen=True)
class CacheInterchangeLimits:
    """Envelope ceilings; row ceilings belong to DESKTOP-123 and are unchanged."""
    raw_input: int = 1024 ** 3
    serialized_export: int = 1024 ** 3
    entries: int = 5_000_000
    namespaces: int = 10_000
    scope_map_entries: int = 10_000
    scope_map_bytes: int = 1024 ** 2
    scope_reference: int = 1024
    depth: int = 8
    row_limits: CacheEntryV2Limits = CACHE_ENTRY_V2_LIMITS

    def __post_init__(self):
        for name, ceiling in (("raw_input", 1024 ** 3), ("serialized_export", 1024 ** 3),
                              ("entries", 5_000_000), ("namespaces", 10_000),
                              ("scope_map_entries", 10_000), ("scope_map_bytes", 1024 ** 2),
                              ("scope_reference", 1024), ("depth", 8)):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                _invalid("invalid_limits")
        if type(self.row_limits) is not CacheEntryV2Limits or self.row_limits != CACHE_ENTRY_V2_LIMITS:
            _invalid("invalid_limits")


@dataclass(frozen=True, repr=False)
class CacheNamespace:
    """Caller selection. Payload descriptors cannot construct destination authority."""
    kind: str
    scope: AuthorizedCacheScope | None = None

    def __post_init__(self):
        if (self.kind not in ("legacy-v1", "v2", "all") or
                (self.kind == "v2" and type(self.scope) is not AuthorizedCacheScope) or
                (self.kind != "v2" and self.scope is not None)):
            _invalid("invalid_namespace")
        if self.scope is not None:
            try:
                if len(self.scope.scope.scope_id.encode("utf-8")) > 1024:
                    _invalid("scope_reference_limit")
            except UnicodeError:
                _invalid("invalid_namespace")

    @classmethod
    def legacy(cls): return cls("legacy-v1")
    @classmethod
    def v2(cls, scope): return cls("v2", scope)
    @classmethod
    def all(cls): return cls("all")
    def __repr__(self): return f"CacheNamespace(kind={self.kind!r})"


def namespace_reference(namespace: CacheNamespace) -> tuple[str, ...]:
    if namespace.kind == "v2":
        return ("v2", namespace.scope.scope.kind.value, namespace.scope.scope.scope_id)
    return (namespace.kind,)


@dataclass(frozen=True, repr=False)
class CacheScopeMapping:
    source: CacheNamespace
    destination: CacheNamespace

    def __post_init__(self):
        if type(self.source) is not CacheNamespace or type(self.destination) is not CacheNamespace:
            _invalid("invalid_scope_map")
        if self.source.kind == "all" or self.destination.kind == "all":
            _invalid("invalid_scope_map")
        if (self.source.kind == "legacy-v1") != (self.destination.kind == "legacy-v1"):
            _invalid("legacy_scope_isolated")
    def __repr__(self): return "CacheScopeMapping()"


@dataclass(frozen=True, repr=False)
class ImportScopeMap:
    entries: tuple[CacheScopeMapping, ...]

    def __post_init__(self):
        if (type(self.entries) is not tuple or not 1 <= len(self.entries) <= 10_000 or
                any(type(entry) is not CacheScopeMapping for entry in self.entries)):
            _invalid("invalid_scope_map")
        sources = tuple(namespace_reference(item.source) for item in self.entries)
        if len(set(sources)) != len(sources):
            _invalid("duplicate_source_scope")
    def __repr__(self): return f"ImportScopeMap(count={len(self.entries)})"


@dataclass(frozen=True, repr=False)
class CacheNamespaceCount:
    kind: str
    count: int
    scope_kind: str | None = None
    scope_id: str | None = None

    def __post_init__(self):
        if (self.kind not in ("legacy-v1", "v2") or type(self.count) is not int or self.count < 0 or
                (self.kind == "v2" and (self.scope_kind not in ("project", "local_user", "work") or not isinstance(self.scope_id, str))) or
                (self.kind == "legacy-v1" and (self.scope_kind is not None or self.scope_id is not None))):
            _invalid("invalid_summary")
    def __repr__(self): return f"CacheNamespaceCount(kind={self.kind!r}, count={self.count})"


@dataclass(frozen=True, repr=False)
class CacheRef:
    value: str
    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value or "\x00" in self.value:
            _invalid("invalid_cache_ref")
    def __repr__(self):
        return "CacheRef()"


@dataclass(frozen=True, repr=False)
class FileRef:
    value: str
    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value or "\x00" in self.value:
            _invalid("invalid_file_ref")
    def __repr__(self):
        return "FileRef()"


@dataclass(frozen=True, repr=False)
class CacheFilter:
    source_lang: str | None = None
    target_lang: str | None = None
    before: str | None = None
    all_entries: bool = False
    def __post_init__(self):
        for value in (self.source_lang, self.target_lang, self.before):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > MAX_METADATA_LENGTH):
                _invalid("invalid_filter")
        if type(self.all_entries) is not bool or (self.all_entries and any(v is not None for v in (self.source_lang, self.target_lang, self.before))):
            _invalid("invalid_scope")
        if self.before is not None:
            try:
                datetime.fromisoformat(self.before.replace("T", " "))
            except ValueError:
                _invalid("invalid_before")
    def __repr__(self):
        return "CacheFilter()"


@dataclass(frozen=True, repr=False)
class CachePair:
    source_lang: str
    target_lang: str
    count: int
    first_created_at: str
    last_created_at: str
    def __post_init__(self):
        if (type(self.count) is not int or self.count < 0 or
                any(not isinstance(v, str) or len(v) > MAX_METADATA_LENGTH for v in
                    (self.source_lang, self.target_lang, self.first_created_at, self.last_created_at))):
            _invalid("invalid_summary")
    def __repr__(self):
        return f"CachePair(count={self.count})"


@dataclass(frozen=True)
class CacheSummary:
    exists: bool
    count: int
    size_bytes: int
    pairs: tuple[CachePair, ...]
    adapter: str = "legacy_v1"
    layout: str = "sqlite_translations"
    namespace: CacheNamespace | None = field(default=None, repr=False)
    namespaces: tuple[CacheNamespaceCount, ...] = ()
    def __post_init__(self):
        if (type(self.exists) is not bool or type(self.count) is not int or self.count < 0 or
                type(self.size_bytes) is not int or self.size_bytes < 0 or type(self.pairs) is not tuple or
                len(self.pairs) > MAX_ENTRIES or any(type(p) is not CachePair for p in self.pairs) or
                (sum(p.count for p in self.pairs) != self.count and self.adapter == "legacy_v1") or
                (not self.exists and (self.count or self.size_bytes or self.pairs)) or
                (self.adapter, self.layout) not in (("legacy_v1", "sqlite_translations"), ("semantic_v2", "sqlite_cache_v2")) or
                type(self.namespaces) is not tuple or len(self.namespaces) > 10_000 or
                any(type(item) is not CacheNamespaceCount for item in self.namespaces) or
                (self.namespace is not None and type(self.namespace) is not CacheNamespace) or
                (self.namespaces and sum(item.count for item in self.namespaces) != self.count)):
            _invalid("invalid_summary")


@dataclass(frozen=True, repr=False)
class CleanSelection:
    cache: CacheRef
    filters: CacheFilter
    count: int
    digest: str
    namespace: CacheNamespace | None = None
    namespaces: tuple[CacheNamespaceCount, ...] = ()
    def __post_init__(self):
        if (type(self.cache) is not CacheRef or type(self.filters) is not CacheFilter or
            type(self.count) is not int or self.count < 0 or not isinstance(self.digest, str) or
            re.fullmatch(r"[0-9a-f]{64}", self.digest) is None):
            _invalid("invalid_selection")
        if self.namespace is not None and type(self.namespace) is not CacheNamespace:
            _invalid("invalid_namespace")
        if (type(self.namespaces) is not tuple or len(self.namespaces) > 10_000 or
                any(type(item) is not CacheNamespaceCount for item in self.namespaces) or
                (self.namespaces and sum(item.count for item in self.namespaces) != self.count)):
            _invalid("invalid_selection")
        if self.namespace is None:
            _validate_clean_scope(self.filters)
    def __repr__(self):
        return f"CleanSelection(count={self.count})"


def _validate_clean_scope(filters: CacheFilter) -> None:
    if not filters.all_entries and not any(v is not None for v in (filters.source_lang, filters.target_lang, filters.before)):
        _invalid("missing_scope")


@dataclass(frozen=True)
class ImportCounts:
    imported: int
    skipped: int
    replaced: int
    def __post_init__(self):
        if any(type(n) is not int or n < 0 for n in (self.imported, self.skipped, self.replaced)):
            _invalid("invalid_counts")
    @property
    def total_read(self):
        return self.imported + self.skipped + self.replaced


class Cancellation(Protocol):
    def checkpoint(self, safe_point: str) -> object | None: ...


class ProgressSink(Protocol):
    def report(self, progress: CacheProgress) -> None: ...


@dataclass(frozen=True, repr=False)
class InspectRequest:
    cache: CacheRef
    filters: CacheFilter = field(default_factory=CacheFilter)
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None
    namespace: CacheNamespace | None = None
    limits: CacheInterchangeLimits = field(default_factory=CacheInterchangeLimits)
    def __post_init__(self):
        _request(self.cache, self.filters)
        _namespace_request(self.namespace, self.limits)
    def __repr__(self): return "InspectRequest()"


@dataclass(frozen=True, repr=False)
class PreviewCleanRequest(InspectRequest):
    def __post_init__(self):
        super().__post_init__()
        if self.namespace is None:
            _validate_clean_scope(self.filters)
    def __repr__(self): return "PreviewCleanRequest()"


@dataclass(frozen=True, repr=False)
class ExecuteCleanRequest:
    selection: CleanSelection
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None
    def __post_init__(self):
        if type(self.selection) is not CleanSelection: _invalid("invalid_request")
    def __repr__(self): return "ExecuteCleanRequest()"


@dataclass(frozen=True, repr=False)
class ExportRequest:
    cache: CacheRef
    destination: FileRef
    filters: CacheFilter = field(default_factory=CacheFilter)
    overwrite: bool = False
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None
    namespace: CacheNamespace | None = None
    limits: CacheInterchangeLimits = field(default_factory=CacheInterchangeLimits)
    format_version: int | None = None
    def __post_init__(self):
        _request(self.cache, self.filters)
        _namespace_request(self.namespace, self.limits)
        if self.format_version is not None and (type(self.format_version) is not int or self.format_version not in (1, 2)):
            _invalid("unsupported_version")
        if type(self.destination) is not FileRef or type(self.overwrite) is not bool: _invalid("invalid_request")
    def __repr__(self): return "ExportRequest()"


@dataclass(frozen=True, repr=False)
class ImportRequest:
    cache: CacheRef
    source: FileRef
    replace: bool = False
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None
    scope_map: ImportScopeMap | None = None
    limits: CacheInterchangeLimits = field(default_factory=CacheInterchangeLimits)
    preview_revision: ImportPreview | None = None
    def __post_init__(self):
        if type(self.cache) is not CacheRef or type(self.source) is not FileRef or type(self.replace) is not bool: _invalid("invalid_request")
        if self.scope_map is not None and type(self.scope_map) is not ImportScopeMap:
            _invalid("invalid_scope_map")
        _namespace_request(None, self.limits)
    def __repr__(self): return "ImportRequest()"


def _request(cache, filters):
    if type(cache) is not CacheRef or type(filters) is not CacheFilter: _invalid("invalid_request")


def _namespace_request(namespace, limits):
    if namespace is not None and type(namespace) is not CacheNamespace:
        _invalid("invalid_namespace")
    if type(limits) is not CacheInterchangeLimits:
        _invalid("invalid_limits")


@dataclass(frozen=True, repr=False)
class ImportPreview:
    cache: CacheRef
    source: FileRef
    scope_map: ImportScopeMap
    replace: bool
    revision: str
    count: int
    imported: int
    skipped: int
    replaced: int
    conflicts: int
    duplicates: int
    stage: FileRef
    limits: CacheInterchangeLimits = field(default_factory=CacheInterchangeLimits)

    def __post_init__(self):
        if (type(self.cache) is not CacheRef or type(self.source) is not FileRef or
                type(self.stage) is not FileRef or type(self.scope_map) is not ImportScopeMap or
                type(self.replace) is not bool or not isinstance(self.revision, str) or
                not re.fullmatch(r"[0-9a-f]{64}", self.revision) or
                any(type(value) is not int or value < 0 for value in
                    (self.count, self.imported, self.skipped, self.replaced, self.conflicts, self.duplicates))):
            _invalid("invalid_preview")
        _namespace_request(None, self.limits)
    def __repr__(self):
        return f"ImportPreview(count={self.count}, conflicts={self.conflicts}, duplicates={self.duplicates})"


@dataclass(frozen=True, repr=False)
class ExecuteImportRequest:
    preview: ImportPreview
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None

    def __post_init__(self):
        if type(self.preview) is not ImportPreview:
            _invalid("invalid_preview")
    def __repr__(self): return "ExecuteImportRequest()"


T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class CacheResult(Generic[T]):
    value: T | None = None
    failure: JobFailure | None = None
    warnings: tuple[JobWarning, ...] = ()
    namespace: CacheNamespace | None = field(default=None, repr=False)
    format_version: int | None = None
    scope_map: ImportScopeMap | None = field(default=None, repr=False)
    def __post_init__(self):
        if ((self.value is None) == (self.failure is None) or
                (self.failure is not None and type(self.failure) is not JobFailure) or
                type(self.warnings) is not tuple or len(self.warnings) > 16 or
                any(type(warning) is not JobWarning for warning in self.warnings) or
                (self.namespace is not None and type(self.namespace) is not CacheNamespace) or
                (self.scope_map is not None and type(self.scope_map) is not ImportScopeMap) or
                (self.format_version is not None and (type(self.format_version) is not int or self.format_version not in (1, 2)))):
            _invalid("invalid_result")
    def __repr__(self):
        return f"CacheResult(failure={self.failure!r})" if self.failure else "CacheResult(success)"


class CacheRepository(Protocol):
    def inspect(self, request: InspectRequest) -> CacheSummary: ...
    def preview_clean(self, request: PreviewCleanRequest) -> CleanSelection: ...
    def execute_clean(self, request: ExecuteCleanRequest) -> int: ...
    def export_payload(self, request: ExportRequest) -> bytes: ...
    def import_payload(self, request: ImportRequest, payload: bytes) -> ImportCounts: ...
    def export_interchange(self, request: ExportRequest) -> ExportReceipt: ...
    def preview_import(self, request: ImportRequest) -> ImportPreview: ...
    def execute_import(self, request: ExecuteImportRequest) -> ImportCounts: ...
    def discard_import(self, preview: ImportPreview) -> int: ...


@dataclass(frozen=True)
class ExportReceipt:
    count: int
    format_version: int
    def __post_init__(self):
        if type(self.count) is not int or self.count < 0 or type(self.format_version) is not int or self.format_version not in (1, 2):
            _invalid("invalid_port_result")


class CacheFiles(Protocol):
    def read(self, request: ImportRequest) -> bytes: ...
    def publish(self, request: ExportRequest, payload: bytes) -> None: ...


class LegacyCacheAdministration(Protocol):
    """Streaming production implementation of the unchanged JSON-v1 behavior."""
    def export_cache(self, request: ExportRequest) -> ExportReceipt: ...
    def import_cache(self, request: ImportRequest) -> ImportCounts: ...


def checkpoint(request, safe_point):
    if safe_point not in SAFE_POINTS:
        raise CacheAdminError("undeclared_safe_point", JobFailureFamily.INTERNAL)
    token = request.cancellation
    if token is not None:
        try:
            acknowledged = token.checkpoint(safe_point)
        except Exception:
            raise CacheAdminError("cancellation_check_failed", JobFailureFamily.INTERNAL) from None
        if acknowledged is not None:
            raise CacheAdminError("cancelled", JobFailureFamily.CANCELLED)


def report(request, phase, completed, total):
    if request.progress is not None:
        try:
            request.progress.report(CacheProgress(phase, completed, total))
        except Exception:
            raise CacheAdminError("progress_sink_failed", JobFailureFamily.INTERNAL) from None


@dataclass(frozen=True)
class CacheProgress:
    phase: str
    completed: int
    total: int | None
    def __post_init__(self):
        if (not isinstance(self.phase, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.phase) or
            type(self.completed) is not int or self.completed < 0 or
            (self.total is not None and (type(self.total) is not int or self.total < self.completed))):
            _invalid("invalid_progress")


def validate_payload(payload: bytes, request=None) -> int:
    try:
        return _validate_legacy(payload, file_bytes=MAX_FILE_BYTES, entries_limit=MAX_ENTRIES,
            text_length=MAX_TEXT_LENGTH, metadata_length=MAX_METADATA_LENGTH,
            structure_depth=MAX_STRUCTURE_DEPTH, structure_tokens=MAX_STRUCTURE_TOKENS,
            check=(lambda: checkpoint(request, "cache_between_entries")) if request is not None else None)
    except LegacyFormatError as exc:
        _invalid(str(exc))


def _run(fn, expected, *, namespace=None, scope_map=None, format_version=None):
    try:
        value = fn()
        if type(value) is not expected or (expected is int and value < 0):
            raise CacheAdminError("invalid_port_result", JobFailureFamily.INTERNAL)
        return CacheResult(value=value, namespace=namespace, scope_map=scope_map, format_version=format_version)
    except CacheAdminError as exc:
        return CacheResult(failure=JobFailure(exc.family, exc.code), namespace=namespace, scope_map=scope_map)
    except Exception:
        return CacheResult(failure=JobFailure(JobFailureFamily.INTERNAL, "unexpected_error"), namespace=namespace, scope_map=scope_map)


class InspectCache:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: InspectRequest) -> CacheResult[CacheSummary]:
        return _run(lambda: self.repository.inspect(request), CacheSummary, namespace=request.namespace or CacheNamespace.legacy())


class PreviewClean:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: PreviewCleanRequest) -> CacheResult[CleanSelection]:
        def run():
            selection = self.repository.preview_clean(request)
            if (type(selection) is not CleanSelection or selection.cache != request.cache or
                    selection.filters != request.filters or selection.namespace != request.namespace):
                raise CacheAdminError("invalid_port_result", JobFailureFamily.INTERNAL)
            return selection
        return _run(run, CleanSelection, namespace=request.namespace or CacheNamespace.legacy())


class ExecuteClean:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: ExecuteCleanRequest) -> CacheResult[int]:
        return _run(lambda: self.repository.execute_clean(request), int, namespace=request.selection.namespace or CacheNamespace.legacy())


class ExportCache:
    def __init__(self, repository: CacheRepository, files: CacheFiles, legacy: LegacyCacheAdministration | None = None):
        self.repository, self.files, self.legacy = repository, files, legacy
    def execute(self, request: ExportRequest) -> CacheResult[int]:
        if request.namespace is not None or self.legacy is not None:
            receipt = _run(lambda: (self.repository.export_interchange(request) if request.namespace is not None
                                   else self.legacy.export_cache(request)), ExportReceipt)
            if receipt.failure:
                return CacheResult(failure=receipt.failure, namespace=request.namespace or CacheNamespace.legacy())
            return CacheResult(value=receipt.value.count, namespace=request.namespace or CacheNamespace.legacy(),
                               format_version=receipt.value.format_version)
        def run():
            if request.format_version not in (None, 1):
                _invalid("unsupported_version")
            checkpoint(request, "cache_before_read")
            payload = self.repository.export_payload(request)
            count = validate_payload(payload, request)
            self.files.publish(request, payload)
            return count
        return _run(run, int, namespace=CacheNamespace.legacy(), format_version=1)


class ImportCache:
    def __init__(self, repository: CacheRepository, files: CacheFiles, legacy: LegacyCacheAdministration | None = None):
        self.repository, self.files, self.legacy = repository, files, legacy
    def execute(self, request: ImportRequest) -> CacheResult[ImportCounts]:
        def run():
            if request.scope_map is None and self.legacy is not None:
                return self.legacy.import_cache(request)
            checkpoint(request, "cache_before_read")
            if request.scope_map is not None:
                if request.preview_revision is None:
                    _invalid("preview_required")
                preview = request.preview_revision
                if (preview.cache != request.cache or preview.source != request.source or
                        preview.scope_map != request.scope_map or preview.replace != request.replace or
                        preview.limits != request.limits):
                    _invalid("stale_preview")
                return self.repository.execute_import(ExecuteImportRequest(preview, request.cancellation, request.progress))
            payload = self.files.read(request)
            validate_payload(payload, request)
            checkpoint(request, "cache_before_mutation")
            return self.repository.import_payload(request, payload)
        return _run(run, ImportCounts, scope_map=request.scope_map,
                    namespace=CacheNamespace.legacy() if request.scope_map is None else None)


class PreviewImport:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: ImportRequest) -> CacheResult[ImportPreview]:
        def run():
            if request.scope_map is None:
                _invalid("missing_scope_map")
            checkpoint(request, "cache_before_read")
            preview = self.repository.preview_import(request)
            if (type(preview) is not ImportPreview or preview.cache != request.cache or
                    preview.source != request.source or preview.scope_map != request.scope_map or
                    preview.replace != request.replace or preview.limits != request.limits):
                raise CacheAdminError("invalid_port_result", JobFailureFamily.INTERNAL)
            return preview
        return _run(run, ImportPreview, scope_map=request.scope_map)


class ExecuteImport:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: ExecuteImportRequest) -> CacheResult[ImportCounts]:
        return _run(lambda: self.repository.execute_import(request), ImportCounts, scope_map=request.preview.scope_map)


class DiscardImport:
    """Release only a preview artifact owned by the local administration adapter."""
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, preview: ImportPreview) -> CacheResult[int]:
        return _run(lambda: self.repository.discard_import(preview), int)
