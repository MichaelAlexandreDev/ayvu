"""Integration tests for SQLite and atomic cache administration."""
import errno
import hashlib
import json
import os
import sqlite3
import threading

import pytest
from typer.testing import CliRunner

from ayvu import cache_admin, cache_admin_store
from ayvu.cache import CacheKey, TranslationCache
from ayvu.cache_admin import (CacheRef, FileRef, CacheFilter, InspectRequest, PreviewCleanRequest,
    ExecuteCleanRequest, ExportRequest, ImportRequest)
from ayvu.cache_admin_composition import cache_admin_services
from ayvu.cli import app
from ayvu.domain import LanguagePair


def key(text):
    return CacheKey(text=text, language_pair=LanguagePair(source="en", target="pt"))


def seed(path, *texts):
    with TranslationCache(path) as cache:
        for text in texts:
            cache.set(key(text), "translated " + text)


def inspect(services, path):
    return services.inspect.execute(InspectRequest(CacheRef(str(path))))


def test_absent_inspection_and_export_do_not_create_cache(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "absent" / "cache.sqlite"
    result = inspect(services, path)
    assert result.value.exists is False and result.value.count == 0
    target = tmp_path / "export.json"
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(target))))
    assert result.value == 0
    assert json.loads(target.read_text()) == {"version": 1, "entries": []}
    assert not path.parent.exists()


def test_clean_preview_stale_and_success(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    ref = CacheRef(str(path))
    preview = services.preview.execute(PreviewCleanRequest(ref, CacheFilter(source_lang="en"))).value
    assert preview.count == 1 and inspect(services, path).value.count == 1
    seed(path, "two")
    stale = services.clean.execute(ExecuteCleanRequest(preview))
    assert stale.failure.code == "stale_preview"
    assert inspect(services, path).value.count == 2
    preview = services.preview.execute(PreviewCleanRequest(ref, CacheFilter(all_entries=True))).value
    assert services.clean.execute(ExecuteCleanRequest(preview)).value == 2
    assert inspect(services, path).value.count == 0


class CancelAt:
    def __init__(self, point):
        self.point = point
        self.seen = []
    def checkpoint(self, point):
        self.seen.append(point)
        return object() if point == self.point else None


def test_clean_cancel_before_commit_rolls_back(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    cancel = CancelAt("cache_before_commit")
    result = services.clean.execute(ExecuteCleanRequest(preview, cancellation=cancel))
    assert result.failure.code == "cancelled" and result.value is None
    assert cancel.seen[-1] == "cache_before_commit"
    assert inspect(services, path).value.count == 2


def test_export_atomic_replacement_and_failure(tmp_path, monkeypatch):
    services = cache_admin_services()
    path, target = tmp_path / "cache.sqlite", tmp_path / "export.json"
    seed(path, "one")
    target.write_bytes(b"previous")
    request = ExportRequest(CacheRef(str(path)), FileRef(str(target)), overwrite=True)
    import ayvu.cache_admin_store as store
    def fail(*args):
        raise OSError(errno.ENOSPC, "sensitive environment")
    with monkeypatch.context() as patch:
        patch.setattr(store.os, "replace", fail)
        result = services.export.execute(request)
    assert result.failure.code == "disk_full"
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".ayvu-cache-*"))
    cancel = CancelAt("cache_before_publish")
    result = services.export.execute(ExportRequest(request.cache, request.destination, overwrite=True, cancellation=cancel))
    assert result.failure.code == "cancelled" and cancel.seen[-1] == "cache_before_publish"
    assert target.read_bytes() == b"previous"
    assert services.export.execute(request).value == 1
    assert json.loads(target.read_text())["entries"][0]["original_text"] == "one"


def test_replacement_failure_has_distinct_code(tmp_path, monkeypatch):
    services = cache_admin_services()
    path, target = tmp_path / "cache.sqlite", tmp_path / "export.json"
    seed(path, "one")
    target.write_bytes(b"previous")
    import ayvu.cache_admin_store as store
    def fail(*args):
        raise OSError(errno.EIO, "private")
    monkeypatch.setattr(store.os, "replace", fail)
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(target)), overwrite=True))
    assert result.failure.code == "publication_failed"
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".ayvu-cache-*"))


def test_import_conflicts_and_cancel_before_commit(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "one")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    request = ImportRequest(CacheRef(str(dest)), FileRef(str(output)))
    cancel = CancelAt("cache_before_commit")
    result = services.import_cache.execute(ImportRequest(request.cache, request.source, cancellation=cancel))
    assert result.failure.code == "cancelled" and cancel.seen[-1] == "cache_before_commit"
    assert inspect(services, dest).value.count == 1
    counts = services.import_cache.execute(request).value
    assert (counts.imported, counts.skipped, counts.replaced) == (1, 1, 0)
    counts = services.import_cache.execute(ImportRequest(request.cache, request.source, replace=True)).value
    assert (counts.imported, counts.skipped, counts.replaced) == (0, 0, 2)


def test_import_progress_failure_rolls_back_prior_insert(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "existing")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    class Sink:
        def __init__(self): self.seen = []
        def report(self, progress):
            self.seen.append(progress.completed)
            if progress.completed == 1: raise RuntimeError("private")
    sink = Sink()
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output)), progress=sink))
    assert sink.seen == [0, 1]
    assert result.failure.code == "progress_sink_failed"
    assert inspect(services, dest).value.count == 1


def test_import_cancellation_during_payload_validation_precedes_mutation(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 1
    cancel = CancelAt("cache_between_entries")
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output)), cancellation=cancel))
    assert result.failure.code == "cancelled"
    assert cancel.seen == ["cache_before_read", "cache_between_entries"]
    assert not dest.exists()


def test_busy_lock_is_distinct_and_keeps_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_admin_store, "BUSY_TIMEOUT_SECONDS", 0.1)
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    with sqlite3.connect(path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        result = services.clean.execute(ExecuteCleanRequest(preview))
        assert result.failure.code == "cache_busy"
    assert inspect(services, path).value.count == 1


def test_permission_failure_is_sanitized(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    import ayvu.cache_admin_store as store
    def fail(*args, **kwargs):
        raise PermissionError("secret path")
    monkeypatch.setattr(store.tempfile, "mkstemp", fail)
    result = services.export.execute(ExportRequest(CacheRef(str(path)), FileRef(str(tmp_path / "out"))))
    assert result.failure.code == "permission_denied"
    assert "secret" not in repr(result)


def test_import_sql_failure_rolls_back_prior_insert(tmp_path):
    services = cache_admin_services()
    source, dest, output = tmp_path / "source.sqlite", tmp_path / "dest.sqlite", tmp_path / "data.json"
    seed(source, "one", "two")
    seed(dest, "existing")
    assert services.export.execute(ExportRequest(CacheRef(str(source)), FileRef(str(output)))).value == 2
    with sqlite3.connect(dest) as conn:
        conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON translations WHEN NEW.original_text = 'two' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(output))))
    assert result.failure is not None and result.value is None
    with TranslationCache(dest) as cache:
        assert cache.get(key("one")) is None
        assert cache.get(key("two")) is None
        assert cache.get(key("existing")) == "translated existing"


@pytest.mark.parametrize("data,expected", [(b"{", "malformed_json"),
    (b'{"version":2,"entries":[]}', "unsupported_version"),
    (b'{"version":1,"entries":[{}]}', "malformed_entry")])
def test_bad_import_never_creates_cache(tmp_path, data, expected):
    services = cache_admin_services()
    source, dest = tmp_path / "bad.json", tmp_path / "absent" / "cache.sqlite"
    source.write_bytes(data)
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(source))))
    assert result.failure.code == expected
    assert not dest.parent.exists()


def test_bounded_file_read_rejects_oversized_input(tmp_path, monkeypatch):
    import ayvu.cache_admin_store as store
    services = cache_admin_services()
    source, dest = tmp_path / "large.json", tmp_path / "cache.sqlite"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 5)
    result = services.import_cache.execute(ImportRequest(CacheRef(str(dest)), FileRef(str(source))))
    assert result.failure.code == "limit_exceeded"
    assert not dest.exists()


@pytest.mark.parametrize("filters,expected", [
    (CacheFilter(all_entries=True), 3),
    (CacheFilter(source_lang="en"), 2),
    (CacheFilter(target_lang="pt"), 2),
    (CacheFilter(before="2024-02-01"), 1),
    (CacheFilter(source_lang="en", target_lang="pt", before="2024-02-01"), 1),
])
def test_clean_filters_select_exact_rows(tmp_path, filters, expected):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    with TranslationCache(path) as cache:
        cache.set(key("one"), "um")
        cache.set(key("two"), "dois")
        cache.set(CacheKey(text="three", language_pair=LanguagePair(source="es", target="es")), "tres")
        cache.connection.execute("UPDATE translations SET created_at = CASE WHEN original_text = 'one' THEN '2024-01-01' ELSE '2024-03-01' END")
        cache.connection.commit()
    ref = CacheRef(str(path))
    preview = services.preview.execute(PreviewCleanRequest(ref, filters)).value
    assert preview.count == expected
    assert services.clean.execute(ExecuteCleanRequest(preview)).value == expected
    assert inspect(services, path).value.count == 3 - expected


def test_cli_absent_inspect_and_export_compatibility(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ayvu.cli import app
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "absent" / "cache.sqlite"
    runner = CliRunner()
    result = runner.invoke(app, ["cache", "inspect", "--cache", str(cache)])
    assert result.exit_code == 0, result.output
    assert "Cache path:" in result.output and "No cache entries found." in result.output
    assert not cache.parent.exists()
    result = runner.invoke(app, ["cache", "export", "empty.json", "--cache", str(cache)])
    assert result.exit_code == 0, result.output
    assert "Cache export saved to:" in result.output and "Entries exported: 0" in result.output
    assert json.loads((tmp_path / "empty.json").read_text()) == {"version": 1, "entries": []}
    assert not cache.parent.exists()


def test_cli_rejects_cache_as_export_destination(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ayvu.cli import app
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "cache.sqlite"
    seed(cache, "one")
    before = cache.read_bytes()
    result = CliRunner().invoke(app, ["cache", "export", str(cache), "--cache", str(cache), "--overwrite"])
    assert result.exit_code == 1
    assert "destination_is_cache" in result.output
    assert cache.read_bytes() == before
    assert inspect(cache_admin_services(), cache).value.count == 1


def _export(services, cache, destination, overwrite=False):
    return services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(destination)), CacheFilter(), overwrite))


def _empty_sqlite_file(path):
    path.write_bytes(b"")


def _sqlite_with_another_table(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other (a)")


@pytest.mark.parametrize("make", [_empty_sqlite_file, _sqlite_with_another_table], ids=["empty_file", "no_table"])
def test_a_cache_without_the_table_is_empty_and_is_left_alone(tmp_path, make):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    make(path)
    before = path.read_bytes()

    summary = inspect(services, path)
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True)))
    cleaned = services.clean.execute(ExecuteCleanRequest(preview.value))
    exported = _export(services, path, tmp_path / "out.json")

    assert summary.failure is None and summary.value.count == 0
    assert preview.value.count == 0 and cleaned.value == 0
    assert exported.value == 0 and json.loads((tmp_path / "out.json").read_text())["entries"] == []
    assert path.read_bytes() == before


def test_something_that_is_not_sqlite_is_still_reported_as_corrupt(tmp_path):
    path = tmp_path / "cache.sqlite"
    path.write_bytes(b"not sqlite at all" * 100)

    assert inspect(cache_admin_services(), path).failure.code == "corrupt_cache"


def test_a_failed_first_import_leaves_a_cache_that_still_opens(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    entry = {"source_lang": "en", "target_lang": "pt", "original_text": "Hi", "translated_text": "Oi",
             "original_text_hash": hashlib.sha256(b"Hi").hexdigest(), "created_at": "2024-01-01T00:00:00+00:00"}
    payload = tmp_path / "in.json"
    payload.write_text(json.dumps({"version": 1, "entries": [entry]}))
    source = CacheRef(str(path))

    class Stop:
        def checkpoint(self, point):
            return object() if point == "cache_before_commit" else None

    failed = services.import_cache.execute(ImportRequest(source, FileRef(str(payload)), False, cancellation=Stop()))

    assert failed.failure.code == "cancelled"
    summary = inspect(services, path)
    assert summary.failure is None and summary.value.count == 0


def test_a_lock_held_for_a_moment_is_waited_for(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True))).value
    holding, release = threading.Event(), threading.Event()

    def hold():
        connection = sqlite3.connect(path)
        connection.execute("BEGIN IMMEDIATE")
        holding.set()
        release.wait(5)
        connection.rollback()
        connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    threading.Timer(0.4, release.set).start()

    result = services.clean.execute(ExecuteCleanRequest(preview))
    holder.join()

    assert result.failure is None and result.value == 1


def test_cleaning_is_the_way_out_of_a_row_above_the_interchange_limits(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    monkeypatch.setattr(cache_admin_store, "MAX_TEXT_LENGTH", 1)

    preview = services.preview.execute(PreviewCleanRequest(CacheRef(str(path)), CacheFilter(all_entries=True)))
    cleaned = services.clean.execute(ExecuteCleanRequest(preview.value))

    assert preview.failure is None and cleaned.value == 2


def test_both_layers_agree_on_the_limits():
    assert cache_admin.MAX_FILE_BYTES == cache_admin_store.MAX_FILE_BYTES
    assert cache_admin.MAX_ENTRIES == cache_admin_store.MAX_ENTRIES
    assert cache_admin.MAX_TEXT_LENGTH == cache_admin_store.MAX_TEXT_LENGTH


def test_a_cache_of_a_real_library_round_trips(tmp_path):
    """Ten or so books. The first limits, 64 MiB and 100 thousand entries, refused it."""
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    count = 110_000
    with TranslationCache(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO translations (source_lang, target_lang, original_text_hash, original_text, translated_text, created_at) "
            "VALUES ('en', 'pt', ?, ?, ?, '2024-01-01T00:00:00+00:00')",
            ((hashlib.sha256(str(index).encode()).hexdigest(), str(index), "PT") for index in range(count)))
    out = tmp_path / "out.json"

    exported = _export(services, path, out)
    imported = services.import_cache.execute(ImportRequest(CacheRef(str(tmp_path / "other.sqlite")), FileRef(str(out))))

    assert exported.value == count
    assert imported.value.imported == count


def test_an_export_follows_the_umask_like_any_other_output(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    out = tmp_path / "out.json"
    previous = os.umask(0o027)
    try:
        assert _export(services, path, out).failure is None
    finally:
        os.umask(previous)

    assert out.stat().st_mode & 0o777 == 0o640
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_replacing_an_export_keeps_what_the_old_file_allowed(tmp_path):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    out = tmp_path / "out.json"
    out.write_text("old")
    out.chmod(0o604)

    assert _export(services, path, out, overwrite=True).failure is None

    assert out.stat().st_mode & 0o777 == 0o604
    assert out.read_text() != "old"


def _no_links(code):
    def link(*_args, **_kwargs):
        raise OSError(code, "no hard links here")
    return link


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_an_export_reaches_a_filesystem_without_hard_links(tmp_path, monkeypatch, code):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(code))
    out = tmp_path / "out.json"

    assert _export(services, path, out).value == 1

    assert len(json.loads(out.read_text())["entries"]) == 1
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_without_hard_links_an_existing_export_is_still_not_replaced(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(errno.EPERM))
    out = tmp_path / "out.json"
    out.write_text("mine")

    assert _export(services, path, out).failure.code == "already_exists"

    assert out.read_text() == "mine"
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_a_link_refused_for_another_reason_is_still_a_failure(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(errno.EACCES))
    out = tmp_path / "out.json"

    assert _export(services, path, out).failure.code == "permission_denied"
    assert not out.exists()


def test_the_command_says_how_to_get_past_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    monkeypatch.setattr(cache_admin_store, "MAX_ENTRIES", 1)
    out = tmp_path / "out.json"

    result = CliRunner().invoke(app, ["cache", "export", str(out), "--cache", str(path)])

    assert result.exit_code == 1
    assert "--before" in " ".join(result.output.split())
    assert not out.exists()


def test_the_command_says_when_another_process_holds_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(cache_admin_store, "BUSY_TIMEOUT_SECONDS", 0.05)
    with sqlite3.connect(path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        result = CliRunner().invoke(app, ["cache", "clean", "--all", "--yes", "--cache", str(path)])

    assert result.exit_code == 1
    assert "Outro processo" in " ".join(result.output.split())


@pytest.mark.parametrize("command", ["export", "import"])
def test_commands_wait_while_another_process_writes_the_cache(tmp_path, monkeypatch, command):
    # A writer holds the cache when the command starts. The command must wait for it, as before
    # DESKTOP-127, instead of failing on the read-only schema check. The writer finishes on the
    # first wait, so the test needs no real sleep.
    from ayvu import cache_schema
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "cache.sqlite"
    seed(path, "one", "two")
    source = tmp_path / "in.json"
    assert CliRunner().invoke(app, ["cache", "export", str(source), "--cache", str(path)]).exit_code == 0
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    waits = []

    def finish_writer(seconds):
        waits.append(seconds)
        if writer.in_transaction:
            writer.execute("ROLLBACK")

    monkeypatch.setattr(cache_schema.time, "sleep", finish_writer)
    args = {"export": ["cache", "export", str(tmp_path / "out.json")],
            "import": ["cache", "import", str(source), "--replace"]}[command]
    try:
        result = CliRunner().invoke(app, [*args, "--cache", str(path)])
    finally:
        writer.close()

    assert result.exit_code == 0, result.output
    assert waits, "the command did not meet the held lock"
    assert "Outro processo" not in result.output


def test_waiting_schema_check_still_reports_a_cache_that_stays_busy(tmp_path, monkeypatch):
    from ayvu import cache_schema
    clock = iter([0.0, 0.0, 0.2, 0.4, 0.6])
    monkeypatch.setattr(cache_schema.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cache_schema.time, "sleep", lambda seconds: None)
    calls = []

    def busy(path, *, authorized_directory=None):
        calls.append(path)
        raise cache_schema.CacheSchemaError("cache_busy")

    monkeypatch.setattr(cache_schema, "classify_cache", busy)
    with pytest.raises(cache_schema.CacheSchemaError) as raised:
        cache_schema.classify_cache_waiting(tmp_path / "c.sqlite", timeout=0.5)
    assert raised.value.code == "cache_busy"
    assert len(calls) == 4
