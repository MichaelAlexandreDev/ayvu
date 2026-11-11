import errno
import hashlib
import io
import json
import os
from dataclasses import replace
from types import SimpleNamespace
import pytest
from ayvu.knowledge.cache.admin import (CacheAdminError, CacheInterchangeLimits, CacheNamespace,
    CacheRef, CacheScopeMapping, ExecuteImport, ExecuteImportRequest, ExportCache,
    ExportRequest, FileRef, ImportRequest, ImportScopeMap, PreviewImport)
from ayvu.composition.builders.cache_admin import NamespaceCacheStore
from ayvu.knowledge.cache.admin_v2_store import SQLiteCacheV2Store
from ayvu.knowledge.cache import admin_v2_export as export_module
from ayvu.knowledge.cache.interchange import iter_records, write_envelope
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.cache.schema import prepare_cache, participating_connection
from tests.support.cache_repository_v2_support import entry

def legacy(text):
    return dict(source_lang="en", target_lang="pt", original_text_hash=hashlib.sha256(text.encode()).hexdigest(), original_text=text, translated_text="ok", created_at="2026")


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "cache.sqlite"
    prepare_cache(path)
    ns = CacheNamespace.legacy()
    mapping = ImportScopeMap((CacheScopeMapping(ns, ns),))
    return SimpleNamespace(path=path, ns=ns, mapping=mapping, store=NamespaceCacheStore(), dir=tmp_path)


def rows(path):
    with participating_connection(path) as conn:
        return conn.execute("SELECT source_lang,target_lang,original_text_hash,original_text,translated_text,created_at FROM translations ORDER BY id").fetchall()


def src(env, entries):
    path = env.dir / "source.json"
    path.write_text(json.dumps({"version": 2, "namespaces": [{"kind": "legacy-v1", "entries": entries}]}))
    return path


def request(env, path, **kwargs):
    return ImportRequest(CacheRef(str(env.path)), FileRef(str(path)), scope_map=env.mapping, **kwargs)
