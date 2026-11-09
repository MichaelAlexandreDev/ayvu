"""Composition root for local cache administration."""
from dataclasses import dataclass
import os
import stat

from .cache_admin import (ExecuteClean, ExportCache, ImportCache, InspectCache, PreviewClean,
                          PreviewImport, ExecuteImport, DiscardImport, ExportReceipt)
from .cache_admin_store import LocalCacheFiles, SQLiteCacheStore
from .cache_admin_v2_store import SQLiteCacheV2Store
from .cache_admin_legacy_store import SQLiteLegacyStreamingStore
from . import cache_admin_store as legacy_store
from .cache_schema import SchemaState, classify_cache_waiting, _SUFFIXES
from pathlib import Path
from dataclasses import replace
from .cache_admin import CacheAdminError, CacheNamespace, checkpoint


def prepare_export_destination(request):
    path, cache = Path(request.destination.value).absolute(), Path(request.cache.value).absolute()
    protected = (cache, *(cache.with_name(cache.name + suffix) for suffix in _SUFFIXES))
    if path.resolve() in (item.resolve() for item in protected):
        raise CacheAdminError('destination_is_cache')
    missing, parent = [], path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for ancestor in (parent, *parent.parents):
        if not stat.S_ISDIR(ancestor.lstat().st_mode):
            raise CacheAdminError('unsafe_cache_path')
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)


def read_scope_map_payload(reference):
    """Read only a caller-selected regular, unaliased local authority file."""
    try:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError()
        path = Path(reference.value).absolute()
        for parent in (path.parent, *path.parent.parents):
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024**2:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
                raise ValueError()
            raw = stream.read(1024**2 + 1)
            end = os.fstat(stream.fileno())
        after = path.lstat()
        if (len(raw) > 1024**2 or (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns) !=
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) or
                (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError()
        return raw
    except (OSError, ValueError):
        raise CacheAdminError("invalid_scope_map") from None


class NamespaceCacheStore:
    """Dispatch only from the caller's selection; never from payload text."""
    def __init__(self):
        self.legacy, self.semantic = SQLiteCacheStore(), SQLiteCacheV2Store()
        self.streaming_legacy = SQLiteLegacyStreamingStore()

    def _selected(self, cache, namespace):
        if namespace is None:
            return self.legacy
        state = classify_cache_waiting(Path(cache.value), timeout=legacy_store.BUSY_TIMEOUT_SECONDS)
        if namespace.kind == "v2" and state not in (SchemaState.V2, SchemaState.ABSENT):
            raise CacheAdminError("namespace_unavailable")
        return self.semantic

    def inspect(self, request):
        store = self._selected(request.cache, request.namespace)
        summary = store.inspect(request)
        return replace(summary, namespace=request.namespace) if store is self.legacy else summary

    def preview_clean(self, request):
        store = self._selected(request.cache, request.namespace)
        selection = store.preview_clean(request)
        return replace(selection, namespace=request.namespace) if store is self.legacy else selection

    def execute_clean(self, request):
        return self._selected(request.selection.cache, request.selection.namespace).execute_clean(request)

    def export_cache(self, request):
        try:
            state = classify_cache_waiting(Path(request.cache.value), timeout=legacy_store.BUSY_TIMEOUT_SECONDS)
            if state in (SchemaState.V2, SchemaState.PARTIAL):
                raise CacheAdminError("v2-admin-required")
            if state is SchemaState.CORRUPT:
                raise CacheAdminError("corrupt_cache")
            prepare_export_destination(request)
            return self.semantic.export_legacy(replace(request, namespace=CacheNamespace.legacy()))
        except Exception as exc:
            raise legacy_store._error(exc) from None

    def import_cache(self, request): return self.streaming_legacy.import_cache(request)

    def export_payload(self, request): return self.legacy.export_payload(request)
    def import_payload(self, request, payload): return self.legacy.import_payload(request, payload)

    def export_interchange(self, request):
        store = self._selected(request.cache, request.namespace)
        if store is self.semantic:
            try:
                prepare_export_destination(request)
                return store.export_interchange(request)
            except Exception as exc:
                raise legacy_store._error(exc) from None
        # A selected legacy namespace keeps #150's exact JSON-v1 behavior.
        payload = self.legacy.export_payload(request)
        if request.format_version not in (None, 1):
            raise CacheAdminError("unsupported_version")
        from .cache_admin import validate_payload
        count = validate_payload(payload, request)
        LocalCacheFiles().publish(request, payload)
        return ExportReceipt(count, 1)

    def preview_import(self, request): return self.semantic.preview_import(request)
    def execute_import(self, request): return self.semantic.execute_import(request)
    def discard_import(self, preview): return self.semantic.discard_import(preview)


@dataclass(frozen=True)
class CacheAdminServices:
    inspect: InspectCache
    preview: PreviewClean
    clean: ExecuteClean
    export: ExportCache
    import_cache: ImportCache
    preview_import: PreviewImport
    execute_import: ExecuteImport
    discard_import: DiscardImport


def cache_admin_services() -> CacheAdminServices:
    repository = NamespaceCacheStore()
    files = LocalCacheFiles()
    return CacheAdminServices(InspectCache(repository), PreviewClean(repository),
                              ExecuteClean(repository), ExportCache(repository, files, legacy=repository),
                              ImportCache(repository, files, legacy=repository), PreviewImport(repository), ExecuteImport(repository),
                              DiscardImport(repository))
