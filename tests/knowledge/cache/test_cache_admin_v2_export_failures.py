import errno
import json
import os
from dataclasses import replace
import pytest

from ayvu.knowledge.cache.admin import CacheInterchangeLimits, CacheNamespace, CacheRef, ExportCache, ExportRequest, FileRef, ImportRequest, PreviewImport
from ayvu.composition.builders.cache_admin import NamespaceCacheStore
from ayvu.knowledge.cache.repository import SQLiteExactCache
from tests.support.cache_repository_v2_support import entry

from tests.knowledge.cache.cache_admin_v2_failures_support import env, legacy, request, rows, src


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
    monkeypatch.setattr("os." + operation, broken)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == expected
    assert "SECRET-ERROR" not in repr(result) + caplog.text
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


@pytest.mark.parametrize("site,number,expected", [("mkstemp", errno.ENOSPC, "disk_full"), ("mkstemp", errno.EIO, "file_error"), ("write_envelope", errno.ENOSPC, "disk_full"), ("write_envelope", errno.EIO, "file_error")])
def test_temp_create_and_write_errors_preserve_destination(tmp_path, monkeypatch, caplog, site, number, expected):
    cache = tmp_path / "cache.sqlite"
    item = entry()
    SQLiteExactCache(cache).put_if_absent(item)
    target = tmp_path / "out.json"
    target.write_text("original")
    def broken(*args, **kwargs): raise OSError(number, "SECRET-TEMP")
    module = "tempfile.mkstemp" if site == "mkstemp" else "ayvu.knowledge.cache.admin_v2_export.write_envelope"
    monkeypatch.setattr(module, broken)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True, namespace=CacheNamespace.v2(item.key.scope))
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert result.failure.code == expected
    assert "SECRET-TEMP" not in repr(result) + caplog.text
    assert target.read_text() == "original"
    assert not list(tmp_path.glob('.ayvu-cache-export-*'))


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
    monkeypatch.setattr('os.fsync', failing_fsync)
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
    monkeypatch.setattr('ayvu.knowledge.cache.admin_store.os.link', no_hardlinks)
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
    monkeypatch.setattr('ayvu.knowledge.cache.admin_v2_import.sqlite3.connect', substitute)
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
    monkeypatch.setattr('os.link', no_links)
    monkeypatch.setattr('os.replace', bad_replace)
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
    monkeypatch.setattr('os.fsync', fail_after_rename)
    req = ExportRequest(CacheRef(str(cache)), FileRef(str(target)), overwrite=True,
                        namespace=CacheNamespace.v2(item.key.scope), cancellation=swap)
    result = ExportCache(NamespaceCacheStore(), None).execute(req)
    assert swap.calls == 2
    assert len(directory_calls) == 1
    assert result.failure.code == 'cache_identity_changed'
    assert target.read_bytes() == b'original'
    assert victim.read_bytes() == b'SECRET-VICTIM'
    assert 'SECRET-VICTIM' not in repr(result) + caplog.text


@pytest.mark.parametrize('operation', ['import', 'export'])
@pytest.mark.parametrize('alias', ['symlink', 'hardlink', 'fifo'])
def test_unselected_v1_compatibility_rejects_file_aliases(tmp_path, monkeypatch, operation, alias):
    from ayvu.knowledge.cache import LEGACY_SCHEMA as SCHEMA
    from ayvu.composition.builders.cache_admin import cache_admin_services
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
        monkeypatch.setattr('ayvu.knowledge.cache.legacy_admin_store._file_digest', forbidden_digest)
        result = services.import_cache.execute(ImportRequest(CacheRef(str(cache)), FileRef(str(selected))))
    else:
        result = services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(selected)), overwrite=True))
    assert result.failure.code == 'unsafe_cache_path'
    assert payload.read_bytes() == original
    assert inspected == []
    with sqlite3.connect(cache) as conn:
        assert conn.execute('SELECT COUNT(*) FROM translations').fetchone()[0] == 0


def test_v1_export_still_creates_requested_output_parents(tmp_path):
    from ayvu.composition.builders.cache_admin import cache_admin_services
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
