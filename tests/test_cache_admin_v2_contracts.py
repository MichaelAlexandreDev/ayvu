"""Caller authority and redaction at the framework-free boundary."""
import ast
from dataclasses import replace
from pathlib import Path
from uuid import uuid5, NAMESPACE_DNS
import json
import logging

import pytest

from ayvu import cache_admin as a
from ayvu.cache_identity import (CacheAuthorizationPolicy, CacheScope, CacheScopeKind,
                                 CACHE_ENTRY_V2_LIMITS)


def namespace(name="sensitive-project"):
    return a.CacheNamespace.v2(CacheAuthorizationPolicy(str(uuid5(NAMESPACE_DNS, name))).resolve())


def test_contracts_import_no_json_cli_sqlite_or_paths():
    tree = ast.parse(Path(a.__file__).read_text())
    forbidden = {"json", "sqlite3", "typer", "rich", "pathlib", "os"}
    for node in ast.walk(tree):
        names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        assert not forbidden.intersection(name.split(".")[0] for name in names)


@pytest.mark.parametrize("kind,scope", [("unknown", None), ("v2", None), ("v2", "untrusted"),
                                       ("all", "untrusted"), ("legacy-v1", "untrusted")])
def test_namespace_is_closed(kind, scope):
    with pytest.raises(a.CacheAdminError, match="invalid_namespace"):
        a.CacheNamespace(kind, scope)


def test_payload_scope_cannot_be_destination_authority():
    with pytest.raises(a.CacheAdminError, match="invalid_namespace"):
        a.CacheNamespace.v2(CacheScope(CacheScopeKind.PROJECT, "payload-project"))


def test_map_rejects_duplicate_sources_all_and_legacy_promotion():
    legacy, exact = a.CacheNamespace.legacy(), namespace()
    with pytest.raises(a.CacheAdminError, match="duplicate_source_scope"):
        a.ImportScopeMap((a.CacheScopeMapping(exact, exact), a.CacheScopeMapping(exact, exact)))
    for src, dst, code in ((legacy, exact, "legacy_scope_isolated"),
                            (exact, legacy, "legacy_scope_isolated"),
                            (a.CacheNamespace.all(), exact, "invalid_scope_map"),
                            (exact, a.CacheNamespace.all(), "invalid_scope_map")):
        with pytest.raises(a.CacheAdminError, match=code):
            a.CacheScopeMapping(src, dst)


def test_many_to_one_mapping_is_explicit():
    one, two, dest = namespace("one"), namespace("two"), namespace("dest")
    mapping = a.ImportScopeMap((a.CacheScopeMapping(one, dest), a.CacheScopeMapping(two, dest)))
    assert len(mapping.entries) == 2


@pytest.mark.parametrize("name,ceiling", [("raw_input", 1024**3), ("serialized_export", 1024**3),
    ("entries", 5_000_000), ("namespaces", 10_000), ("scope_map_entries", 10_000),
    ("scope_map_bytes", 1024**2), ("scope_reference", 1024), ("depth", 8)])
def test_envelope_limits_cannot_be_implicitly_raised(name, ceiling):
    assert getattr(a.CacheInterchangeLimits(**{name: ceiling}), name) == ceiling
    with pytest.raises(a.CacheAdminError, match="invalid_limits"):
        a.CacheInterchangeLimits(**{name: ceiling + 1})
    with pytest.raises(a.CacheAdminError, match="invalid_limits"):
        a.CacheInterchangeLimits(**{name: True})


def test_row_limits_are_owned_by_schema_dependency():
    assert a.CacheInterchangeLimits().row_limits is CACHE_ENTRY_V2_LIMITS
    with pytest.raises(a.CacheAdminError, match="invalid_limits"):
        a.CacheInterchangeLimits(row_limits=replace(CACHE_ENTRY_V2_LIMITS, value=1))


def test_requests_previews_and_namespace_counts_redact_correlators():
    ns = namespace()
    mapping = a.ImportScopeMap((a.CacheScopeMapping(ns, ns),))
    cache, source, stage = a.CacheRef("sensitive-cache"), a.FileRef("sensitive-source"), a.FileRef("sensitive-stage")
    preview = a.ImportPreview(cache, source, mapping, True, "a" * 64, 1, 1, 0, 0, 0, 0, stage)
    values = (ns, mapping, a.InspectRequest(cache, namespace=ns),
              a.ExportRequest(cache, source, namespace=ns), a.ImportRequest(cache, source, scope_map=mapping),
              preview, a.ExecuteImportRequest(preview),
              a.CacheNamespaceCount("v2", 1, "project", "sensitive-project"),
              a.CleanSelection(cache, a.CacheFilter(), 1, "a" * 64, ns))
    for value in values:
        assert "sensitive" not in repr(value)
        assert ns.scope.scope.scope_id not in repr(value)
        assert "a" * 64 not in repr(value)


def test_import_cannot_execute_without_preview_and_never_touches_ports():
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError("port must remain untouched")
    ns = namespace()
    request = a.ImportRequest(a.CacheRef("cache"), a.FileRef("input"),
                             scope_map=a.ImportScopeMap((a.CacheScopeMapping(ns, ns),)))
    outcome = a.ImportCache(Untouched(), Untouched()).execute(request)
    assert outcome.failure.code == "preview_required"


def test_changed_replace_policy_is_stale_before_port_access():
    ns = namespace()
    mapping = a.ImportScopeMap((a.CacheScopeMapping(ns, ns),))
    cache, source = a.CacheRef("cache"), a.FileRef("file")
    preview = a.ImportPreview(cache, source, mapping, False, "a" * 64, 1, 1, 0, 0, 0, 0, a.FileRef("stage"))
    request = a.ImportRequest(cache, source, True, scope_map=mapping, preview_revision=preview)
    result = a.ImportCache(object(), object()).execute(request)
    assert result.failure.code == "stale_preview"


def test_scope_map_entry_count_normative_boundary():
    from ayvu.cache_admin_v2_store import _map
    project = str(uuid5(NAMESPACE_DNS, "authorized"))
    dest = namespace()
    def pair(index):
        scope = CacheScope(CacheScopeKind.WORK, str(index))
        src = a.CacheNamespace.v2(CacheAuthorizationPolicy(project, (scope,)).resolve(scope))
        return a.CacheScopeMapping(src, dest)
    rows = tuple(pair(index) for index in range(10_000))
    assert len(_map(a.ImportScopeMap(rows), a.CacheInterchangeLimits())) == 10_000
    with pytest.raises(a.CacheAdminError, match="invalid_scope_map"):
        a.ImportScopeMap((*rows, pair(10_000)))


def test_scope_map_canonical_bytes_normative_boundary():
    from ayvu.cache_admin_v2_store import _map
    project = str(uuid5(NAMESPACE_DNS, "authorized"))
    def ns(text):
        scope = CacheScope(CacheScopeKind.WORK, text)
        return a.CacheNamespace.v2(CacheAuthorizationPolicy(project, (scope,)).resolve(scope))
    dest = ns("d" * 1024)
    rows = []
    # Build full references, then size the last pair to the real 1 MiB ceiling.
    def encoded_size(items):
        value = sorted((list(a.namespace_reference(item.source)), list(a.namespace_reference(item.destination))) for item in items)
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    while True:
        source = ns(f"{len(rows):05d}" + "s" * 1019)
        pair = a.CacheScopeMapping(source, dest)
        if encoded_size((*rows, pair)) > 1024**2:
            break
        rows.append(pair)
    remaining = 1024**2 - encoded_size((*rows, a.CacheScopeMapping(ns("x"), ns("y"))))
    assert 0 <= remaining < 2046
    source_length = min(remaining + 1, 1024)
    dest_length = remaining - source_length + 2
    last_source, last_dest = ns("x" * source_length), ns("y" * dest_length)
    exact = a.ImportScopeMap((*rows, a.CacheScopeMapping(last_source, last_dest)))
    assert encoded_size(exact.entries) == 1024**2
    assert len(_map(exact, a.CacheInterchangeLimits())) == len(exact.entries)
    excessive = a.ImportScopeMap((*rows, a.CacheScopeMapping(last_source, ns("y" * (dest_length + 1)))))
    with pytest.raises(a.CacheAdminError, match="scope_map_limit"):
        _map(excessive, a.CacheInterchangeLimits())


def test_normalized_validation_failure_suppresses_sensitive_exception_context(caplog):
    secret = 'secret-book-https://provider.example/credential'
    with caplog.at_level(logging.ERROR):
        try:
            a.CacheFilter(before=secret)
        except a.CacheAdminError as exc:
            assert exc.code == 'invalid_before'
            logging.getLogger('cache-test').exception('bounded_validation_failure')
    assert 'invalid_before' in caplog.text
    assert secret not in caplog.text
    assert 'provider.example' not in caplog.text
    assert 'credential' not in caplog.text
