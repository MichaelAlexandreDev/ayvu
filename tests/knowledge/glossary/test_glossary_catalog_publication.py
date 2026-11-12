from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ayvu.knowledge.glossary.catalog import create_glossary, list_glossaries, read_glossary, update_glossary
from ayvu.knowledge.glossary.catalog_store import ManagedGlossaryRepository

from tests.knowledge.glossary.glossary_catalog_support import _create_alias_worker, _run_pair, entry
from tests.knowledge.glossary.glossary_catalog_support import ref


@pytest.mark.parametrize("extra, accepted", [(0, True), (1, False)])
def test_serialized_size_boundary(tmp_path, extra, accepted):
    # The adapter's canonical JSON for these entries is exactly 1 MiB before
    # the optional extra character; this also checks the real published bytes.
    additions = [96, 96, 96, 96, 45 + extra]
    entries = tuple(entry(str(i), "x" * (4000 + (additions[i] if i < 5 else 0))) for i in range(258))
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), entries)
    assert (result.failure is None) is accepted
    if accepted:
        assert (tmp_path / "x.json").stat().st_size == 1024 * 1024
    else:
        assert not (tmp_path / "x.json").exists()


def test_cancel_at_publication_preserves_previous_file(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    original = path.read_bytes()
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls == 3
    result = update_glossary(repo, ref("x.json"), (entry(translation="changed"),), created.value.revision, cancel)
    assert calls == 3 and result.failure == "cancelled"
    assert path.read_bytes() == original
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".ayvu-glossary-")]


def test_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    original = path.read_bytes()
    def fail(_fd):
        raise OSError("secret filename disk failure")
    monkeypatch.setattr(os, "fsync", fail)
    result = update_glossary(repo, ref("x.json"), (entry(translation="changed"),), created.value.revision)
    assert result.failure is not None and "secret" not in result.failure
    assert path.read_bytes() == original
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".ayvu-glossary-")]


def test_case_and_unicode_aliases_cannot_create_two_entries(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    assert create_glossary(repo, ref("Café.json"), (entry(),)).failure is None
    for name in ("CAFÉ.json", "Cafe\u0301.json"):
        assert create_glossary(repo, ref(name), (entry(),)).failure is not None
    assert len(list_glossaries(repo).value) == 1


def test_final_permissions_are_restricted(tmp_path):
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), (entry(),))
    assert result.failure is None
    assert (tmp_path / "x.json").stat().st_mode & 0o077 == 0


def test_concurrent_unicode_alias_creates_share_collision_domain(tmp_path):
    failures = _run_pair(_create_alias_worker, [(tmp_path, "Café.json"), (tmp_path, "CAFE\u0301.json")])
    assert sum(failure is None for failure in failures) == 1
    assert len(list_glossaries(ManagedGlossaryRepository(tmp_path)).value) == 1


def test_aliases_are_listed_through_a_handle_opened_under_the_lock(tmp_path, monkeypatch):
    """On overlayfs, a directory handle opened before another process added a file can list stale
    entries. Emulate that: the handle opened on the root lists nothing, so only a fresh handle sees
    the existing alias. Found by the Docker test stage (#261)."""
    repo = ManagedGlossaryRepository(tmp_path)
    assert create_glossary(repo, ref("Café.json"), (entry(),)).failure is None
    real_open, real_listdir = os.open, os.listdir
    root_handles = set()

    def tracking_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == repo._root:
            root_handles.add(descriptor)
        return descriptor

    def stale_listdir(path="."):
        return [] if path in root_handles else real_listdir(path)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "listdir", stale_listdir)
    result = create_glossary(repo, ref("CAFE\u0301.json"), (entry(),))
    monkeypatch.undo()
    assert root_handles
    assert result.failure is not None
    assert sorted(path.name for path in tmp_path.glob("*.json")) == ["Café.json"]


def test_replacement_between_stat_and_open_is_rejected(tmp_path, monkeypatch):
    repo = ManagedGlossaryRepository(tmp_path)
    assert create_glossary(repo, ref("x.json"), (entry(),)).failure is None
    replacement = tmp_path / "replacement.tmp"
    replacement.write_text('{"new":"content"}', encoding="utf-8")
    real_open = os.open
    replaced = False
    def open_with_replacement(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "x.json" and not replaced:
            replaced = True
            os.replace(replacement, tmp_path / "x.json")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", open_with_replacement)
    result = read_glossary(repo, ref("x.json"))
    assert replaced
    assert result.failure is not None and result.value is None


def test_replacement_just_before_the_final_check_is_rejected(tmp_path, monkeypatch):
    """The last point Ayvu can see a swap. After it, only the documented residual window remains."""
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    replacement = tmp_path / "replacement.tmp"
    replacement.write_text('{"other":"writer"}', encoding="utf-8")
    real_fsync, real_replace = os.fsync, os.replace
    replaced = False

    def swap_after_the_staged_file_is_synced(descriptor):
        nonlocal replaced
        result = real_fsync(descriptor)
        if not replaced and stat.S_ISREG(os.fstat(descriptor).st_mode):
            replaced = True
            real_replace(replacement, tmp_path / "x.json")
        return result

    monkeypatch.setattr(os, "fsync", swap_after_the_staged_file_is_synced)
    result = update_glossary(repo, ref("x.json"), (entry(translation="changed"),), created.value.revision)

    assert replaced
    assert result.failure == "stale-revision" and result.value is None
    assert (tmp_path / "x.json").read_text(encoding="utf-8") == '{"other":"writer"}'
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-glossary-")] == []


def test_directory_fsync_failure_preserves_original_bytes(tmp_path, monkeypatch):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    original = path.read_bytes()
    real_fsync = os.fsync
    called = False
    def fail_directory(fd):
        nonlocal called
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            called = True
            raise OSError("injected directory sync failure")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    result = update_glossary(repo, ref("x.json"), (entry(translation="changed"),), created.value.revision)
    assert called and result.failure is not None
    assert path.read_bytes() == original


def test_a_sync_failure_after_the_rename_restores_the_previous_bytes(tmp_path, monkeypatch):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    original = path.read_bytes()
    real_fsync = os.fsync
    directory_syncs = 0

    def fail_the_second_directory_sync(descriptor):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("injected directory sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_the_second_directory_sync)
    result = update_glossary(repo, ref("x.json"), (entry(translation="changed"),), created.value.revision)

    assert directory_syncs == 2 and result.failure is not None
    assert path.read_bytes() == original
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-glossary-")] == []
