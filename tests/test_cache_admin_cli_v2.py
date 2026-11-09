import json

import pytest
from typer.testing import CliRunner

from ayvu.cli import app


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_import_all_is_rejected_before_destination_access(tmp_path, monkeypatch):
    import ayvu.cli as cli
    accessed = []
    def forbidden(*args, **kwargs):
        accessed.append(True)
        raise AssertionError('must reject --all before map, source or destination access')
    monkeypatch.setattr(cli, 'cache_admin_services', forbidden)
    monkeypatch.setattr(cli, '_cache_scope_map', forbidden)
    source = tmp_path / "export.json"
    source.write_text("{}")
    destination = tmp_path / "cache.sqlite"
    result = CliRunner().invoke(app, ["cache", "import", str(source), "--cache", str(destination), "--all"])
    assert result.exit_code != 0
    assert not destination.exists()
    assert accessed == []


def test_invalid_scope_map_is_rejected_before_destination_access(tmp_path):
    source = tmp_path / "export.json"
    source.write_text("{}")
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps([{"source": {"kind": "legacy-v1"}, "destination": {"kind": "legacy-v1"}, "unexpected": 1}]))
    destination = tmp_path / "cache.sqlite"
    result = CliRunner().invoke(app, ["cache", "import", str(source), "--cache", str(destination), "--scope-map", str(mapping)])
    assert result.exit_code != 0
    assert not destination.exists()


def test_project_scope_must_match_project_id(tmp_path):
    destination = tmp_path / "cache.sqlite"
    result = CliRunner().invoke(app, ["cache", "inspect", "--cache", str(destination), "--namespace", "v2", "--scope-kind", "project", "--scope-id", "one", "--project-id", "two"])
    assert result.exit_code != 0
    assert not destination.exists()


def _options(project):
    return ["--namespace", "v2", "--scope-kind", "project", "--scope-id", project, "--project-id", project]


@pytest.fixture
def real_cache(tmp_path, monkeypatch):
    import socket
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    from test_translation_cache_v2 import PROJECT

    def no_network(*args, **kwargs):
        raise AssertionError("cache administration must not use the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    source, destination = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    SQLiteExactCache(source).put_if_absent(entry(value="source translation"))
    SQLiteExactCache(destination).put_if_absent(entry(value="destination translation"))
    output, mapping = tmp_path / "export.json", tmp_path / "map.json"
    mapping.write_text(json.dumps([{"source": {"kind": "v2", "scope_kind": "project", "scope_id": PROJECT},
                                    "destination": {"kind": "v2", "scope_kind": "project", "scope_id": PROJECT, "project_id": PROJECT}}]))
    result = CliRunner().invoke(app, ["cache", "export", str(output), "--cache", str(source), *_options(PROJECT)])
    assert result.exit_code == 0, result.output
    assert "JSON format: v2" in result.output
    return source, destination, output, mapping, PROJECT


def test_v2_inspect_export_and_roundtrip_with_conflict(real_cache):
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    source, destination, output, mapping, project = real_cache
    inspected = CliRunner().invoke(app, ["cache", "inspect", "--cache", str(source), *_options(project)])
    assert inspected.exit_code == 0, inspected.output
    assert "Total: 1 cache entry" in inspected.output
    assert project not in inspected.output
    assert json.loads(output.read_text())["version"] == 2
    args = ["cache", "import", str(output), "--cache", str(destination), "--scope-map", str(mapping)]
    preview = CliRunner().invoke(app, [*args, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "1 conflicts" in preview.output and "0 replacements" in preview.output
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))
    assert SQLiteExactCache(destination).lookup(entry().key).value == "destination translation"
    declined = CliRunner().invoke(app, [*args, "--replace"], input="n\n")
    assert declined.exit_code != 0
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))
    assert SQLiteExactCache(destination).lookup(entry().key).value == "destination translation"
    imported = CliRunner().invoke(app, [*args, "--replace", "--yes"])
    assert imported.exit_code == 0, imported.output
    assert len(list(destination.parent.glob(".ayvu-cache-import-*"))) == 1
    assert SQLiteExactCache(destination).lookup(entry().key).value == "source translation"


def test_export_refuses_cache_alias(real_cache):
    source, _, _, _, project = real_cache
    result = CliRunner().invoke(app, ["cache", "export", str(source), "--cache", str(source), "--overwrite", *_options(project)])
    assert result.exit_code != 0
    assert source.read_bytes().startswith(b"SQLite format 3")


@pytest.mark.parametrize("content", [
    '[]',
    '[{"source":{"kind":"legacy-v1","kind":"legacy-v1"},"destination":{"kind":"legacy-v1"}}]',
    '[{"source":{"kind":"legacy-v1"},"destination":{"kind":"legacy-v1"}}, {"source":{"kind":"legacy-v1"},"destination":{"kind":"legacy-v1"}}]',
    '[{"source":{"kind":"v2","scope_kind":"project"},"destination":{"kind":"legacy-v1"}}]',
])
def test_invalid_closed_map_rejected_before_stage(tmp_path, content):
    source = tmp_path / "export.json"
    source.write_text("{}")
    mapping = tmp_path / "map.json"
    mapping.write_text(content)
    result = CliRunner().invoke(app, ["cache", "import", str(source), "--scope-map", str(mapping)])
    assert result.exit_code != 0
    assert not list(tmp_path.glob(".ayvu-cache-import-*"))


def test_all_import_rejected_before_stage(real_cache):
    _, destination, output, mapping, _ = real_cache
    result = CliRunner().invoke(app, ["cache", "import", str(output), "--cache", str(destination), "--scope-map", str(mapping), "--all"])
    assert result.exit_code != 0
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))


def test_stale_preview_reports_retry(real_cache, monkeypatch):
    import typer
    _, destination, output, mapping, _ = real_cache
    def change_export(*args, **kwargs):
        output.write_text(output.read_text() + " ")
        return True
    monkeypatch.setattr(typer, "confirm", change_export)
    result = CliRunner().invoke(app, ["cache", "import", str(output), "--cache", str(destination), "--scope-map", str(mapping), "--replace"])
    assert result.exit_code != 0
    assert "desatualizada" in result.output
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))


def test_unmapped_source_fails_without_changing_destination(real_cache):
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    _, destination, output, mapping, _ = real_cache
    mapping.write_text(json.dumps([{"source": {"kind": "legacy-v1"}, "destination": {"kind": "legacy-v1"}}]))
    result = CliRunner().invoke(app, ["cache", "import", str(output), "--cache", str(destination), "--scope-map", str(mapping), "--yes"])
    assert result.exit_code != 0
    assert SQLiteExactCache(destination).lookup(entry().key).value == "destination translation"
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))


def test_export_rejects_hard_link_alias(real_cache):
    import os
    source, _, _, _, project = real_cache
    alias = source.parent / "alias.sqlite"
    os.link(source, alias)
    result = CliRunner().invoke(app, ["cache", "export", str(alias), "--cache", str(source), "--overwrite", *_options(project)])
    assert result.exit_code != 0
    assert alias.read_bytes().startswith(b"SQLite format 3")


def test_v2_import_requires_map(real_cache):
    _, destination, output, _, _ = real_cache
    result = CliRunner().invoke(app, ["cache", "import", str(output), "--cache", str(destination)])
    assert result.exit_code != 0
    assert not list(destination.parent.glob(".ayvu-cache-import-*"))


def test_all_namespace_inspect_and_clean_dry_run(real_cache):
    source, _, _, _, _ = real_cache
    inspected = CliRunner().invoke(app, ["cache", "inspect", "--cache", str(source), "--all"])
    assert inspected.exit_code == 0, inspected.output
    assert "Total: 1 cache entry" in inspected.output
    clean = CliRunner().invoke(app, ["cache", "clean", "--cache", str(source), "--namespace", "all", "--all", "--dry-run"])
    assert clean.exit_code == 0, clean.output
    assert "1 cache entry" in clean.output


@pytest.mark.parametrize('kind', ['work', 'local_user'])
def test_cli_roundtrip_broader_scope_with_opaque_non_uuid_reference(tmp_path, kind):
    from dataclasses import replace
    from ayvu.cache_identity import CacheScope, CacheScopeKind, CacheAuthorizationPolicy
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    item = entry()
    scope = CacheScope(CacheScopeKind(kind), 'opaque-scope')
    authorized = CacheAuthorizationPolicy(item.key.scope.project_id, (scope,)).resolve(scope)
    item = replace(item, key=replace(item.key, scope=authorized))
    source, target = tmp_path / 'source.sqlite', tmp_path / 'destination.sqlite'
    SQLiteExactCache(source).put_if_absent(item)
    SQLiteExactCache(target)
    output, mapping = tmp_path / 'data.json', tmp_path / 'map.json'
    options = ['--namespace', 'v2', '--scope-kind', kind, '--scope-id', scope.scope_id,
               '--project-id', authorized.project_id]
    result = CliRunner().invoke(app, ['cache', 'export', str(output), '--cache', str(source), *options])
    assert result.exit_code == 0, result.output
    descriptor = dict(kind='v2', scope_kind=kind, scope_id=scope.scope_id)
    mapping.write_text(json.dumps([dict(source=descriptor, destination={**descriptor, 'project_id': authorized.project_id})]))
    result = CliRunner().invoke(app, ['cache', 'import', str(output), '--cache', str(target), '--scope-map', str(mapping), '--yes'])
    assert result.exit_code == 0, result.output
    assert SQLiteExactCache(target).lookup(item.key).value == item.value


def test_v2_cli_before_preserves_timestamp_microseconds(tmp_path):
    from datetime import UTC, datetime, timedelta
    from ayvu.cache_repository import SQLiteExactCache
    from test_cache_repository_v2 import entry
    source = tmp_path / 'microseconds.sqlite'
    item = entry()
    project = item.key.scope.project_id
    instant = datetime(2026, 11, 5, 10, 11, 12, 345678, UTC)
    SQLiteExactCache(source, clock=lambda: instant).put_if_absent(item)
    for before, count in ((instant, 0), (instant + timedelta(microseconds=1), 1)):
        result = CliRunner().invoke(app, ['cache', 'inspect', '--cache', str(source), *_options(project),
                                         '--before', before.isoformat(timespec='microseconds')])
        assert result.exit_code == 0, result.output
        assert f'Total: {count} cache entr' in result.output


def test_invalid_before_diagnostic_never_echoes_untrusted_content(tmp_path, monkeypatch):
    import ayvu.cli as cli
    original = cli._print_expected_error
    details = []
    def observed(*args, **kwargs):
        details.append(kwargs.get('detail'))
        return original(*args, **kwargs)
    monkeypatch.setattr(cli, '_print_expected_error', observed)
    secret = 'SECRET-BOOK-https://provider.example/token/private'
    result = CliRunner().invoke(app, ['cache', 'inspect', '--cache', str(tmp_path / 'cache.sqlite'), '--before', secret])
    assert result.exit_code == 1
    assert details == ['invalid_before']
    assert secret not in result.output


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_scope_map_link_is_rejected(tmp_path, link_kind):
    import os
    source = tmp_path / "export.json"
    source.write_text("{}")
    original = tmp_path / "original.json"
    original.write_text('[{"source":{"kind":"legacy-v1"},"destination":{"kind":"legacy-v1"}}]')
    alias = tmp_path / "alias.json"
    if link_kind == "symlink":
        alias.symlink_to(original)
    else:
        os.link(original, alias)
    result = CliRunner().invoke(app, ["cache", "import", str(source), "--scope-map", str(alias)])
    assert result.exit_code != 0
    assert not list(tmp_path.glob(".ayvu-cache-import-*"))


def test_explicit_legacy_export_reports_json_v1(tmp_path):
    result = CliRunner().invoke(app, ["cache", "export", str(tmp_path / "out.json"), "--cache", str(tmp_path / "missing.sqlite"), "--namespace", "legacy-v1"])
    assert result.exit_code == 0, result.output
    assert "JSON format: v1" in result.output
