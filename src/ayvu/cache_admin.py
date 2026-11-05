"""Framework-independent administration of the standalone v1 cache.

References and payloads are deliberately excluded from representations. Ports own
transaction boundaries and atomic publication; services never open local files.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from .job_events import JobFailure, JobFailureFamily, JobWarning

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
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "unexpected_error"
        self.code, self.family = code, family
        super().__init__(code)


def _invalid(code: str) -> None:
    raise CacheAdminError(code, JobFailureFamily.INPUT)


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
    def __post_init__(self):
        if (type(self.exists) is not bool or type(self.count) is not int or self.count < 0 or
                type(self.size_bytes) is not int or self.size_bytes < 0 or type(self.pairs) is not tuple or
                len(self.pairs) > MAX_ENTRIES or any(type(p) is not CachePair for p in self.pairs) or
                sum(p.count for p in self.pairs) != self.count or
                (not self.exists and (self.count or self.size_bytes or self.pairs)) or
                self.adapter != "legacy_v1" or self.layout != "sqlite_translations"):
            _invalid("invalid_summary")


@dataclass(frozen=True, repr=False)
class CleanSelection:
    cache: CacheRef
    filters: CacheFilter
    count: int
    digest: str
    def __post_init__(self):
        if (type(self.cache) is not CacheRef or type(self.filters) is not CacheFilter or
            type(self.count) is not int or self.count < 0 or not isinstance(self.digest, str) or
            re.fullmatch(r"[0-9a-f]{64}", self.digest) is None):
            _invalid("invalid_selection")
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
    def __post_init__(self):
        _request(self.cache, self.filters)
    def __repr__(self): return "InspectRequest()"


@dataclass(frozen=True, repr=False)
class PreviewCleanRequest(InspectRequest):
    def __post_init__(self):
        super().__post_init__()
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
    def __post_init__(self):
        _request(self.cache, self.filters)
        if type(self.destination) is not FileRef or type(self.overwrite) is not bool: _invalid("invalid_request")
    def __repr__(self): return "ExportRequest()"


@dataclass(frozen=True, repr=False)
class ImportRequest:
    cache: CacheRef
    source: FileRef
    replace: bool = False
    cancellation: Cancellation | None = None
    progress: ProgressSink | None = None
    def __post_init__(self):
        if type(self.cache) is not CacheRef or type(self.source) is not FileRef or type(self.replace) is not bool: _invalid("invalid_request")
    def __repr__(self): return "ImportRequest()"


def _request(cache, filters):
    if type(cache) is not CacheRef or type(filters) is not CacheFilter: _invalid("invalid_request")


T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class CacheResult(Generic[T]):
    value: T | None = None
    failure: JobFailure | None = None
    warnings: tuple[JobWarning, ...] = ()
    def __post_init__(self):
        if ((self.value is None) == (self.failure is None) or
                (self.failure is not None and type(self.failure) is not JobFailure) or
                type(self.warnings) is not tuple or len(self.warnings) > 16 or
                any(type(warning) is not JobWarning for warning in self.warnings)):
            _invalid("invalid_result")
    def __repr__(self):
        return f"CacheResult(failure={self.failure!r})" if self.failure else "CacheResult(success)"


class CacheRepository(Protocol):
    def inspect(self, request: InspectRequest) -> CacheSummary: ...
    def preview_clean(self, request: PreviewCleanRequest) -> CleanSelection: ...
    def execute_clean(self, request: ExecuteCleanRequest) -> int: ...
    def export_payload(self, request: ExportRequest) -> bytes: ...
    def import_payload(self, request: ImportRequest, payload: bytes) -> ImportCounts: ...


class CacheFiles(Protocol):
    def read(self, request: ImportRequest) -> bytes: ...
    def publish(self, request: ExportRequest, payload: bytes) -> None: ...


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
    if not isinstance(payload, bytes): _invalid("invalid_payload")
    if len(payload) > MAX_FILE_BYTES: _invalid("file_too_large")
    # Bound the parser's nesting and object/list structure before it allocates
    # the decoded tree. Brackets and separators inside strings do not count.
    depth = tokens = 0
    quoted = escaped = False
    for byte in payload:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            tokens += 1
            if depth > MAX_STRUCTURE_DEPTH: _invalid("structure_too_deep")
        elif byte in (125, 93):
            depth -= 1
        elif byte in (44, 58):
            tokens += 1
        if tokens > MAX_STRUCTURE_TOKENS: _invalid("structure_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: _invalid("malformed_json")
            result[key] = value
        return result
    try:
        data = json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda _: _invalid("malformed_json"))
    except (ValueError, UnicodeError, RecursionError):
        _invalid("malformed_json")
    if not isinstance(data, dict): _invalid("malformed_payload")
    if type(data.get("version")) is not int or data["version"] != 1: _invalid("unsupported_version")
    entries = data.get("entries")
    if not isinstance(entries, list): _invalid("malformed_payload")
    if len(entries) > MAX_ENTRIES: _invalid("too_many_entries")
    fields = ("source_lang", "target_lang", "original_text_hash", "original_text", "translated_text", "created_at")
    for entry in entries:
        if request is not None:
            checkpoint(request, "cache_between_entries")
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) for k in fields): _invalid("malformed_entry")
        for key in fields:
            if len(entry[key]) > (MAX_TEXT_LENGTH if key in ("original_text", "translated_text") else MAX_METADATA_LENGTH): _invalid("text_too_large")
        try:
            digest = hashlib.sha256(entry["original_text"].encode("utf-8")).hexdigest()
            for key in fields:
                entry[key].encode("utf-8")
        except UnicodeEncodeError:
            _invalid("malformed_entry")
        if digest != entry["original_text_hash"]: _invalid("invalid_text_hash")
    return len(entries)


def _run(fn, expected):
    try:
        value = fn()
        if type(value) is not expected or (expected is int and value < 0):
            raise CacheAdminError("invalid_port_result", JobFailureFamily.INTERNAL)
        return CacheResult(value=value)
    except CacheAdminError as exc:
        return CacheResult(failure=JobFailure(exc.family, exc.code))
    except Exception:
        return CacheResult(failure=JobFailure(JobFailureFamily.INTERNAL, "unexpected_error"))


class InspectCache:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: InspectRequest) -> CacheResult[CacheSummary]:
        return _run(lambda: self.repository.inspect(request), CacheSummary)


class PreviewClean:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: PreviewCleanRequest) -> CacheResult[CleanSelection]:
        def run():
            selection = self.repository.preview_clean(request)
            if (type(selection) is not CleanSelection or selection.cache != request.cache or
                    selection.filters != request.filters):
                raise CacheAdminError("invalid_port_result", JobFailureFamily.INTERNAL)
            return selection
        return _run(run, CleanSelection)


class ExecuteClean:
    def __init__(self, repository: CacheRepository): self.repository = repository
    def execute(self, request: ExecuteCleanRequest) -> CacheResult[int]:
        return _run(lambda: self.repository.execute_clean(request), int)


class ExportCache:
    def __init__(self, repository: CacheRepository, files: CacheFiles): self.repository, self.files = repository, files
    def execute(self, request: ExportRequest) -> CacheResult[int]:
        def run():
            checkpoint(request, "cache_before_read")
            payload = self.repository.export_payload(request)
            count = validate_payload(payload, request)
            self.files.publish(request, payload)
            return count
        return _run(run, int)


class ImportCache:
    def __init__(self, repository: CacheRepository, files: CacheFiles): self.repository, self.files = repository, files
    def execute(self, request: ImportRequest) -> CacheResult[ImportCounts]:
        def run():
            checkpoint(request, "cache_before_read")
            payload = self.files.read(request)
            validate_payload(payload, request)
            checkpoint(request, "cache_before_mutation")
            return self.repository.import_payload(request, payload)
        return _run(run, ImportCounts)
