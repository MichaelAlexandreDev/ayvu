import os

import pytest

from ayvu.knowledge.cache import schema as schema

from tests.knowledge.cache.cache_schema_v2_support import _classified_then_stopped, assert_v2, contents, make_v1


@pytest.mark.parametrize("sidecar", ["", "-journal"])
def test_a_crash_during_the_snapshot_is_redone_on_reopen(tmp_path, sidecar):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    _classified_then_stopped(p)
    partial = schema._partial_backup(p)
    partial.write_bytes(b"half a database")
    if sidecar:
        partial.with_name(partial.name + sidecar).write_bytes(b"journal")

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert contents(schema._backup(p)) == rows
    assert not any(item.exists() for item in schema._partial_backup_files(p))


def test_a_crash_right_before_publishing_the_snapshot_is_redone(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)

    def die(*_args, **_kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(schema.os, "rename", die)
        with pytest.raises(BaseException):
            schema.prepare_cache(p)
    assert schema._partial_backup(p).exists() and not schema._backup(p).exists()

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert contents(schema._backup(p)) == rows
    assert not any(item.exists() for item in schema._partial_backup_files(p))


def test_a_crash_right_after_publishing_the_snapshot_keeps_it(tmp_path, monkeypatch):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    rows = contents(p)
    real_flush = schema._flush_directory

    def die_once_the_backup_exists(path):
        if schema._backup(p).exists():
            raise KeyboardInterrupt
        return real_flush(path)

    with monkeypatch.context() as patch:
        patch.setattr(schema, "_flush_directory", die_once_the_backup_exists)
        with pytest.raises(BaseException):
            schema.prepare_cache(p)
    assert schema._backup(p).exists() and schema._backup(p).stat().st_nlink == 1
    identity = schema._backup(p).stat().st_ino

    schema.prepare_cache(p)

    assert_v2(p, rows)
    assert schema._backup(p).stat().st_ino == identity


def test_a_partial_snapshot_without_a_marker_is_not_taken_as_ours(tmp_path):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    partial = schema._partial_backup(p)
    partial.write_bytes(b"someone else's file")

    with pytest.raises(schema.CacheSchemaError) as exc:
        schema.prepare_cache(p)

    assert exc.value.code == "cache_backup_exists"
    assert partial.read_bytes() == b"someone else's file"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_an_alias_as_partial_snapshot_is_refused_and_left_alone(tmp_path, kind):
    p = tmp_path / "cache.sqlite"
    make_v1(p)
    _classified_then_stopped(p)
    outside = tmp_path / "outside"
    outside.write_bytes(b"precious")
    partial = schema._partial_backup(p)
    if kind == "symlink":
        partial.symlink_to(outside)
    else:
        os.link(outside, partial)

    with pytest.raises(schema.CacheSchemaError):
        schema.prepare_cache(p)

    assert outside.read_bytes() == b"precious"
