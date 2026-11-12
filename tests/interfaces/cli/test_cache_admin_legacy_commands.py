import json
import sqlite3

import pytest
from typer.testing import CliRunner

from ayvu.knowledge.cache import admin_store as cache_admin_store
from ayvu.composition.builders.cache_admin import cache_admin_services
from ayvu.interfaces.cli.app import app

from tests.support.cache_admin_integration_support import inspect, seed


def test_cli_absent_inspect_and_export_compatibility(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ayvu.interfaces.cli.app import app
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
    from ayvu.interfaces.cli.app import app
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
    from ayvu.knowledge.cache import schema as cache_schema
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
    from ayvu.knowledge.cache import schema as cache_schema
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
