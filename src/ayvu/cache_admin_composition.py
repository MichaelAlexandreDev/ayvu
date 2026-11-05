"""Composition root for local cache administration."""
from dataclasses import dataclass

from .cache_admin import ExecuteClean, ExportCache, ImportCache, InspectCache, PreviewClean
from .cache_admin_store import LocalCacheFiles, SQLiteCacheStore


@dataclass(frozen=True)
class CacheAdminServices:
    inspect: InspectCache
    preview: PreviewClean
    clean: ExecuteClean
    export: ExportCache
    import_cache: ImportCache


def cache_admin_services() -> CacheAdminServices:
    repository = SQLiteCacheStore()
    files = LocalCacheFiles()
    return CacheAdminServices(InspectCache(repository), PreviewClean(repository),
                              ExecuteClean(repository), ExportCache(repository, files),
                              ImportCache(repository, files))
