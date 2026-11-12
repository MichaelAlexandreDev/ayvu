import errno
import io
import json
import os
from types import SimpleNamespace
import pytest

from ayvu.knowledge.cache.admin import (CacheAdminError, CacheInterchangeLimits, CacheNamespace,
    CacheRef, CacheScopeMapping, ExecuteImport, ExecuteImportRequest, ExportCache,
    ExportRequest, FileRef, ImportRequest, ImportScopeMap, PreviewImport)
from ayvu.composition.builders.cache_admin import NamespaceCacheStore
from ayvu.knowledge.cache import admin_v2_export as export_module
from ayvu.knowledge.cache.interchange import iter_records, write_envelope
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.cache.schema import participating_connection
from tests.support.cache_repository_v2_support import entry

from tests.knowledge.cache.cache_admin_v2_failures_support import env, legacy, request, rows, src


def test_import_cancel_at_commit_rolls_back(env):
    path = src(env, [legacy("first"), legacy("second")])
    preview = PreviewImport(env.store).execute(request(env, path)).value
    assert preview is not None
    before = rows(env.path)
    class Cancel:
        def checkpoint(self, point): return True if point == "cache_before_commit" else None
    result = ExecuteImport(env.store).execute(ExecuteImportRequest(preview, cancellation=Cancel()))
    assert result.failure.code == "cancelled"
    assert rows(env.path) == before
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


def test_import_refuses_oversize_before_parser_or_stage(env, monkeypatch):
    path = src(env, [legacy("one")])
    monkeypatch.setattr("ayvu.knowledge.cache.admin_v2_records.iter_records", lambda *a: pytest.fail("parser invoked"))
    result = PreviewImport(env.store).execute(request(env, path, limits=CacheInterchangeLimits(raw_input=path.stat().st_size - 1)))
    assert result.failure.code == "file_too_large"
    assert rows(env.path) == []
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "symlink_parent"])
@pytest.mark.parametrize("mode", ["import", "export"])
def test_unsafe_file_types_rejected(env, kind, mode):
    source = src(env, [])
    target = env.dir / "target"
    if kind == "symlink": target.symlink_to(source)
    elif kind == "hardlink": os.link(source, target)
    elif kind == "fifo": os.mkfifo(target)
    elif kind == "directory": target.mkdir()
    else:
        parent = env.dir / "alias"
        parent.symlink_to(env.dir, target_is_directory=True)
        target = parent / "target"
    if mode == "import":
        result = PreviewImport(env.store).execute(request(env, target))
    else:
        req = ExportRequest(CacheRef(str(env.path)), FileRef(str(target)), namespace=env.ns, overwrite=True)
        result = ExportCache(env.store, None).execute(req)
    assert result.failure.code == "unsafe_cache_path"
    assert rows(env.path) == []
    assert not list(env.dir.glob('.ayvu-cache-import-*'))
    assert not list(env.dir.glob('.ayvu-cache-export-*'))


def test_multibatch_import_staging(env, monkeypatch):
    path = src(env, [legacy(str(i) + 'x' * 5000) for i in range(25)])
    assert path.stat().st_size > 65536
    import ayvu.knowledge.cache.admin_v2_records as store_module
    real = store_module.iter_records
    passes = []
    class BoundedStream:
        def __init__(self, stream):
            self.stream = stream
            self.read_sizes = []
        def read(self, size):
            # Instrument the transport, including EOF reads: a full-file read
            # must fail even if the parser still yields entries one at a time.
            assert 0 < size <= 65536
            self.read_sizes.append(size)
            return self.stream.read(size)
    def tracked(stream, *args):
        transport = BoundedStream(stream)
        positions = []
        passes.append((transport, positions))
        for record in real(transport, *args):
            positions.append(stream.tell())
            yield record
    monkeypatch.setattr(store_module, 'iter_records', tracked)
    preview = PreviewImport(env.store).execute(request(env, path)).value
    assert preview is not None and preview.count == 25
    # Complete authority preflight must precede staging. Both that verification
    # and the subsequent staging pass must stay bounded and yield before EOF.
    assert len(passes) == 2
    for transport, positions in passes:
        assert len(positions) == 26
        assert positions[1] < path.stat().st_size
        assert len(transport.read_sizes) >= 3
    result = ExecuteImport(env.store).execute(ExecuteImportRequest(preview))
    assert result.value.imported == 25
    assert len(rows(env.path)) == 25


@pytest.mark.parametrize('parse_pass', [1, 2])
def test_import_rejects_content_changed_only_while_parsing(env, monkeypatch, parse_pass):
    import ayvu.knowledge.cache.admin_v2_records as module
    source = src(env, [legacy('one')])
    original = source.read_bytes()
    changed = original.replace(b'"translated_text": "ok"', b'"translated_text": "no"')
    assert changed != original and len(changed) == len(original)
    before = env.path.read_bytes()
    real, calls = module.iter_records, 0
    def exchange(stream, *args):
        nonlocal calls
        calls += 1
        if calls != parse_pass:
            yield from real(stream, *args)
            return
        source.write_bytes(changed)
        try:
            yield from real(stream, *args)
        finally:
            source.write_bytes(original)
    monkeypatch.setattr(module, 'iter_records', exchange)
    result = PreviewImport(env.store).execute(request(env, source))
    if result.value is not None:
        env.store.discard_import(result.value)
    assert result.failure is not None
    assert result.failure.code == 'stale_preview'
    assert env.path.read_bytes() == before
    assert source.read_bytes() == original
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


def test_v2_legacy_section_limit_both_directions(monkeypatch):
    import ayvu.knowledge.cache.interchange as interchange
    monkeypatch.setattr(interchange, 'MAX_ENTRIES', 2)
    desc = {"kind": "legacy-v1"}
    items = [legacy(str(i)) for i in range(3)]
    out = io.BytesIO()
    write_envelope(out, [(desc, items[:2])], SimpleNamespace())
    assert len(list(iter_records(io.BytesIO(out.getvalue()), SimpleNamespace()))) == 3
    with pytest.raises(CacheAdminError, match="too_many_entries"):
        write_envelope(io.BytesIO(), [(desc, items)], SimpleNamespace())
    raw = json.dumps({"version": 2, "namespaces": [{**desc, "entries": items}]}).encode()
    with pytest.raises(CacheAdminError, match="too_many_entries"):
        list(iter_records(io.BytesIO(raw), SimpleNamespace()))


def test_corrupt_db_refused_before_import_and_stage(env):
    source = src(env, [legacy("one")])
    env.path.write_bytes(b"not-a-sqlite-database")
    before = env.path.read_bytes()
    result = PreviewImport(env.store).execute(request(env, source))
    assert result.failure.code == "corrupt_cache"
    assert env.path.read_bytes() == before
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


def test_cancel_preview_during_multibatch_leaves_database_unchanged(env):
    source = src(env, [legacy(str(i) + 'x' * 5000) for i in range(25)])
    before = rows(env.path)
    class Cancel:
        calls = 0
        def checkpoint(self, point):
            if point == 'cache_between_entries':
                self.calls += 1
                return True if self.calls == 3 else None
    cancel = Cancel()
    result = PreviewImport(env.store).execute(request(env, source, cancellation=cancel))
    assert result.failure.code == 'cancelled'
    assert cancel.calls == 3
    assert rows(env.path) == before
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


@pytest.mark.parametrize("number,expected", [(errno.ENOSPC, 'disk_full'), (errno.EIO, 'file_error')])
def test_import_stage_create_failure(env, monkeypatch, caplog, number, expected):
    source = src(env, [legacy("one")])
    def broken(*args, **kwargs): raise OSError(number, "SECRET-STAGE")
    monkeypatch.setattr('tempfile.mkstemp', broken)
    result = PreviewImport(env.store).execute(request(env, source))
    assert result.failure.code == expected
    assert 'SECRET-STAGE' not in repr(result) + caplog.text
    assert rows(env.path) == []
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


def test_import_rejects_changed_stage_symlink(env):
    source = src(env, [legacy("one")])
    preview = PreviewImport(env.store).execute(request(env, source)).value
    stage = env.dir / os.path.basename(preview.stage.value)
    stage.unlink()
    stage.symlink_to(source)
    result = ExecuteImport(env.store).execute(ExecuteImportRequest(preview))
    assert result.failure.code == 'unsafe_cache_path'
    assert rows(env.path) == []
    assert source.exists()


def test_corrupt_existing_v2_row_refused_before_replace(tmp_path, caplog):
    cache = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    namespace = CacheNamespace.v2(item.key.scope)
    source = tmp_path / 'source.json'
    store = NamespaceCacheStore()
    exported = ExportCache(store, None).execute(ExportRequest(CacheRef(str(cache)), FileRef(str(source)), namespace=namespace))
    assert exported.value == 1
    with participating_connection(cache) as conn:
        conn.execute("UPDATE cache_entries_v2 SET entry_digest='sha256:SECRET-CORRUPT'")
        conn.commit()
        before = conn.execute('SELECT * FROM cache_entries_v2').fetchall()
    mapping = ImportScopeMap((CacheScopeMapping(namespace, namespace),))
    request = ImportRequest(CacheRef(str(cache)), FileRef(str(source)), replace=True, scope_map=mapping)
    result = PreviewImport(store).execute(request)
    assert result.failure.code == 'integrity_failure'
    with participating_connection(cache) as conn:
        assert conn.execute('SELECT * FROM cache_entries_v2').fetchall() == before
    assert not list(tmp_path.glob('.ayvu-cache-import-*'))
    assert 'SECRET-CORRUPT' not in repr(result) + caplog.text


def test_integrity_check_failure_after_writes_rolls_back(env, monkeypatch, caplog):
    from contextlib import contextmanager
    import ayvu.knowledge.cache.admin_v2_import as module
    path = src(env, [legacy('one'), legacy('two')])
    preview = PreviewImport(env.store).execute(request(env, path)).value
    original = module._import_connection
    observed = []
    class CorruptReply:
        def fetchone(self): return ('SECRET-INTEGRITY-DETAIL',)
    class Connection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def execute(self, sql, *args):
            if sql == 'PRAGMA main.quick_check':
                assert self.connection.execute('SELECT COUNT(*) FROM translations').fetchone()[0] == 2
                observed.append('integrity_after_writes')
                return CorruptReply()
            return self.connection.execute(sql, *args)
    @contextmanager
    def injected(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield Connection(connection)
    monkeypatch.setattr(module, '_import_connection', injected)
    result = ExecuteImport(env.store).execute(ExecuteImportRequest(preview))
    assert result.failure.code == 'integrity_failure'
    assert observed == ['integrity_after_writes']
    assert rows(env.path) == []
    assert not list(env.dir.glob('.ayvu-cache-import-*'))
    assert 'SECRET-INTEGRITY-DETAIL' not in repr(result) + caplog.text


def test_trailing_parse_failure_after_staging_never_touches_database(env):
    path = src(env, [legacy('one'), legacy('two')])
    path.write_bytes(path.read_bytes() + b' SECRET-TRAILING')
    before = env.path.read_bytes()
    result = PreviewImport(env.store).execute(request(env, path))
    assert result.failure.code == 'malformed_json'
    assert env.path.read_bytes() == before
    assert not list(env.dir.glob('.ayvu-cache-import-*'))


def test_oversized_database_row_refused_before_text_materialization(tmp_path, monkeypatch):
    from ayvu.knowledge.cache.admin import InspectRequest
    import ayvu.knowledge.cache.admin_v2_store as module
    path = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(path).put_if_absent(item)
    with participating_connection(path) as conn:
        conn.execute('UPDATE cache_entries_v2 SET value=?', ('x' * (1024**2 + 1),))
        conn.commit()
    accessed = []
    def forbidden(*args):
        accessed.append('materialized')
        raise AssertionError('oversized rows must be rejected before row materialization')
    monkeypatch.setattr(module, '_selection', forbidden)
    monkeypatch.setattr(export_module, '_sections', forbidden)
    namespace = CacheNamespace.v2(item.key.scope)
    store = NamespaceCacheStore()
    with pytest.raises(CacheAdminError, match='text_too_large'):
        store.inspect(InspectRequest(CacheRef(str(path)), namespace=namespace))
    target = tmp_path / 'out.json'
    target.write_text('prior')
    result = ExportCache(store, None).execute(ExportRequest(CacheRef(str(path)), FileRef(str(target)),
                                                         overwrite=True, namespace=namespace))
    assert result.failure.code == 'text_too_large'
    assert target.read_text() == 'prior'
    assert accessed == []
