import errno
import hashlib
import io
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ayvu.cache_admin import (CacheAdminError, CacheInterchangeLimits, CacheNamespace,
    CacheRef, CacheScopeMapping, ExecuteImport, ExecuteImportRequest, ExportCache,
    ExportRequest, FileRef, ImportRequest, ImportScopeMap, PreviewImport)
from ayvu.cache_admin_composition import NamespaceCacheStore
from ayvu.cache_admin_v2_store import SQLiteCacheV2Store
from ayvu.cache_interchange import iter_records, write_envelope
from ayvu.cache_repository import SQLiteExactCache
from ayvu.cache_schema import prepare_cache, participating_connection
from test_cache_repository_v2 import entry


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


def test_export_size_failure_preserves_existing_target_and_stage(tmp_path):
    cache = tmp_path / "v2.sqlite"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    ns = CacheNamespace.v2(item.key.scope)
    target = tmp_path / "out.json"
    target.write_text("original")
    service = ExportCache(NamespaceCacheStore(), None)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=ns)
    assert service.execute(req).value == 1
    size = target.stat().st_size
    target.write_text("original")
    assert service.execute(replace(req, limits=CacheInterchangeLimits(serialized_export=size))).value == 1
    target.write_text("original")
    failed = service.execute(replace(req, limits=CacheInterchangeLimits(serialized_export=size - 1)))
    assert failed.failure.code == "file_too_large"
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


@pytest.mark.parametrize("point", ["cache_between_entries", "cache_before_publish"])
def test_export_cancellation_preserves_target(tmp_path, point):
    cache = tmp_path / "v2.sqlite"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / "out.json"
    target.write_text("original")
    class Cancel:
        def checkpoint(self, value): return True if value == point else None
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True,
                        namespace=CacheNamespace.v2(item.key.scope), cancellation=Cancel())
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == "cancelled"
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


@pytest.mark.parametrize("operation,number,expected", [("fsync", errno.ENOSPC, "disk_full"), ("fsync", errno.EIO, "file_error"), ("replace", errno.ENOSPC, "disk_full"), ("replace", errno.EIO, "file_error")])
def test_export_os_errors_redacted_and_atomic(tmp_path, monkeypatch, caplog, operation, number, expected):
    cache = tmp_path / "cache.sqlite"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / "out.json"
    target.write_text("original")
    def broken(*args): raise OSError(number, "SECRET-ERROR")
    monkeypatch.setattr("ayvu.cache_admin_v2_store.os." + operation, broken)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == expected
    assert "SECRET-ERROR" not in repr(result) + caplog.text
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


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
    monkeypatch.setattr("ayvu.cache_admin_v2_store.iter_records", lambda *a: pytest.fail("parser invoked"))
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
    import ayvu.cache_admin_v2_store as store_module
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
    import ayvu.cache_admin_v2_store as module
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
    import ayvu.cache_interchange as interchange
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


@pytest.mark.parametrize("site,number,expected", [("mkstemp", errno.ENOSPC, "disk_full"), ("mkstemp", errno.EIO, "file_error"), ("write_envelope", errno.ENOSPC, "disk_full"), ("write_envelope", errno.EIO, "file_error")])
def test_temp_create_and_write_errors_preserve_destination(tmp_path, monkeypatch, caplog, site, number, expected):
    cache = tmp_path / "cache.sqlite"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / "out.json"
    target.write_text("original")
    def broken(*args, **kwargs): raise OSError(number, "SECRET-TEMP")
    module = "ayvu.cache_admin_v2_store.tempfile.mkstemp" if site == "mkstemp" else "ayvu.cache_admin_v2_store.write_envelope"
    monkeypatch.setattr(module, broken)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == expected
    assert "SECRET-TEMP" not in repr(result) + caplog.text
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


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
    monkeypatch.setattr('ayvu.cache_admin_v2_store.tempfile.mkstemp', broken)
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


@pytest.mark.parametrize('existing', [True, False])
def test_post_publication_directory_sync_failure_restores_target(tmp_path, monkeypatch, existing):
    import stat
    cache = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / 'out.json'
    if existing:
        target.write_bytes(b'original bytes')
        target.chmod(0o640)
    real_fsync = os.fsync
    directory_calls = []
    def failing_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls.append(fd)
            if len(directory_calls) == 2:
                raise OSError(errno.EIO, 'sync failure')
        return real_fsync(fd)
    monkeypatch.setattr('ayvu.cache_admin_v2_store.os.fsync', failing_fsync)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=existing,
                        namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == 'file_error'
    assert len(directory_calls) == 3
    if existing:
        assert target.read_bytes() == b'original bytes'
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    else:
        assert not target.exists()
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


def test_no_hardlink_fallback_publishes_with_umask_mode(tmp_path, monkeypatch):
    import stat
    cache = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    probe = tmp_path / 'probe'
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    os.close(fd)
    expected_mode = stat.S_IMODE(probe.stat().st_mode)
    target = tmp_path / 'out.json'
    calls = []
    def no_hardlinks(*args, **kwargs):
        calls.append(1)
        raise OSError(errno.ENOTSUP, 'unsupported')
    monkeypatch.setattr('ayvu.cache_admin_store.os.link', no_hardlinks)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.value == 1
    assert calls
    assert json.loads(target.read_bytes())['version'] == 2
    assert stat.S_IMODE(target.stat().st_mode) == expected_mode
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


def test_substituted_preview_stage_cannot_modify_victim(env, monkeypatch, caplog):
    import sqlite3
    from pathlib import Path
    victim = env.dir / 'victim.sqlite'
    with sqlite3.connect(victim) as conn:
        conn.execute('CREATE TABLE private_data (secret TEXT)')
        conn.execute('INSERT INTO private_data VALUES (?)', ('SECRET-VICTIM',))
    before = victim.read_bytes()
    source = src(env, [legacy('one')])
    real_connect = sqlite3.connect
    swapped = []
    def substitute(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        if kwargs.get('uri') and '.ayvu-cache-import-' in str(database):
            path = next(env.dir.glob('.ayvu-cache-import-*.sqlite'))
            path.unlink()
            path.symlink_to(victim)
            swapped.append(path)
        return connection
    monkeypatch.setattr('ayvu.cache_admin_v2_store.sqlite3.connect', substitute)
    result = PreviewImport(env.store).execute(request(env, source))
    assert swapped and result.failure.code == 'cache_identity_changed'
    assert victim.read_bytes() == before
    assert rows(env.path) == []
    assert 'SECRET-VICTIM' not in repr(result) + caplog.text


def test_no_hardlinks_replace_failure_removes_placeholder(tmp_path, monkeypatch, caplog):
    cache = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / 'new.json'
    def no_links(*args, **kwargs): raise OSError(errno.ENOTSUP, 'SECRET-LINK')
    def bad_replace(*args, **kwargs): raise OSError(errno.EIO, 'SECRET-REPLACE')
    monkeypatch.setattr('ayvu.cache_admin_v2_store.os.link', no_links)
    monkeypatch.setattr('ayvu.cache_admin_v2_store.os.replace', bad_replace)
    result = ExportCache(NamespaceCacheStore(), None).execute(ExportRequest(
        CacheRef(str(cache)), FileRef(str(target)), namespace=CacheNamespace.v2(item.key.scope)))
    assert result.failure.code == 'file_error'
    assert not target.exists()
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))
    assert 'SECRET-' not in repr(result) + caplog.text


def test_substituted_publication_backup_rejected_before_publish(tmp_path, caplog, monkeypatch):
    import stat
    cache = tmp_path / 'cache.sqlite'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / 'target.json'
    target.write_bytes(b'original')
    victim = tmp_path / 'victim'
    victim.write_bytes(b'SECRET-VICTIM')
    class Swap:
        calls = 0
        def checkpoint(self, point):
            if point == 'cache_before_publish':
                self.calls += 1
                if self.calls == 2:
                    backup = next(tmp_path.glob('.ayvu-cache-export-rollback-*'))
                    backup.unlink()
                    backup.symlink_to(victim)
    swap = Swap()
    real_fsync = os.fsync
    directory_calls = []
    def fail_after_rename(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls.append(fd)
            if len(directory_calls) == 2:
                raise OSError(errno.EIO, 'SECRET-SYNC')
        return real_fsync(fd)
    monkeypatch.setattr('ayvu.cache_admin_v2_store.os.fsync', fail_after_rename)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True,
                        namespace=CacheNamespace.v2(item.key.scope), cancellation=swap)
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert swap.calls == 2
    assert len(directory_calls) == 1
    assert result.failure.code == 'cache_identity_changed'
    assert target.read_bytes() == b'original'
    assert victim.read_bytes() == b'SECRET-VICTIM'
    assert 'SECRET-VICTIM' not in repr(result) + caplog.text


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
    import ayvu.cache_admin_v2_store as module
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
    from ayvu.cache_admin import InspectRequest
    import ayvu.cache_admin_v2_store as module
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
    monkeypatch.setattr(module, '_sections', forbidden)
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


@pytest.mark.parametrize('operation', ['import', 'export'])
@pytest.mark.parametrize('alias', ['symlink', 'hardlink', 'fifo'])
def test_unselected_v1_compatibility_rejects_file_aliases(tmp_path, monkeypatch, operation, alias):
    from ayvu.cache import SCHEMA
    from ayvu.cache_admin_composition import cache_admin_services
    import sqlite3
    cache, payload, selected = tmp_path / 'legacy.sqlite', tmp_path / 'file.json', tmp_path / 'selected.json'
    with sqlite3.connect(cache) as conn: conn.execute(SCHEMA)
    payload.write_text(json.dumps(dict(version=1, entries=[legacy('one')])))
    original = payload.read_bytes()
    if alias == 'symlink': selected.symlink_to(payload)
    elif alias == 'hardlink': os.link(payload, selected)
    else: os.mkfifo(selected)
    services = cache_admin_services()
    inspected = []
    if operation == 'import':
        def forbidden_digest(*args, **kwargs):
            inspected.append('digest')
            raise AssertionError('alias must be refused before reading or staging')
        monkeypatch.setattr('ayvu.cache_admin_legacy_store._file_digest', forbidden_digest)
        result = services.import_cache.execute(ImportRequest(CacheRef(str(cache)), FileRef(str(selected))))
    else:
        result = services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(selected)), overwrite=True))
    assert result.failure.code == 'unsafe_cache_path'
    assert payload.read_bytes() == original
    assert inspected == []
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT COUNT(*) FROM translations').fetchone()[0] == 0


def test_v1_export_still_creates_requested_output_parents(tmp_path):
    from ayvu.cache_admin_composition import cache_admin_services
    cache, output = tmp_path / 'absent-cache' / 'legacy.sqlite', tmp_path / 'new-output' / 'nested' / 'out.json'
    result = cache_admin_services().export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(output))))
    assert result.value == 0
    assert json.loads(output.read_text()) == dict(version=1, entries=[])
    assert not cache.parent.exists()


def test_target_hardlinked_after_backup_is_refused_before_publication(tmp_path):
    cache, output, alias = tmp_path / 'cache.sqlite', tmp_path / 'out.json', tmp_path / 'alias.json'
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    output.write_bytes(b'prior target')
    class LinkBeforePublish:
        calls = 0
        def checkpoint(self, point):
            if point == 'cache_before_publish':
                self.calls += 1
                if self.calls == 2: os.link(output, alias)
            return None
    token = LinkBeforePublish()
    result = ExportCache(NamespaceCacheStore(), None).execute(ExportRequest(CacheRef(str(cache)), FileRef(str(output)),
        overwrite=True, namespace=CacheNamespace.v2(item.key.scope), cancellation=token))
    assert token.calls == 2
    assert result.failure.code == 'unsafe_cache_path'
    assert output.read_bytes() == alias.read_bytes() == b'prior target'
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))
