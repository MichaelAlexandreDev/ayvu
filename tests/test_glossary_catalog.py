"""Contract and local-adapter tests for the managed glossary catalog."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat

import pytest

from ayvu.glossary_catalog import (
    Entry, GlossaryReference, create_glossary, list_glossaries, read_glossary,
    update_glossary,
)
from ayvu.glossary_catalog_store import ManagedGlossaryRepository


def ref(name):
    return GlossaryReference(name)


def entry(term="wizard", translation="mago"):
    return Entry(term, "translate", translation)


def test_create_read_update_and_unchanged(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("magic.json"), (entry(),))
    assert created.failure is None
    loaded = read_glossary(repo, ref("magic.json"))
    assert loaded.failure is None
    assert loaded.value.entries == (entry(),)
    assert loaded.value.revision == created.value.revision
    before = (tmp_path / "magic.json").stat()
    unchanged = update_glossary(repo, ref("magic.json"), (entry(),), loaded.value.revision)
    assert unchanged.failure is None
    assert unchanged.outcome == "already-current"
    assert (tmp_path / "magic.json").stat().st_ino == before.st_ino
    changed = update_glossary(repo, ref("magic.json"), (entry(translation="feiticeiro"),), loaded.value.revision)
    assert changed.failure is None
    stale = update_glossary(repo, ref("magic.json"), (entry(translation="bruxo"),), loaded.value.revision)
    assert stale.failure == "stale-revision"
    assert read_glossary(repo, ref("magic.json")).value.entries == (entry(translation="feiticeiro"),)


def test_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"existing":"original"}', encoding="utf-8")
    original = path.read_bytes()
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), (entry(),))
    assert result.failure is not None
    assert path.read_bytes() == original


def test_deterministic_list_and_bad_files_have_no_content(tmp_path):
    (tmp_path / "b.json").write_text('{"x":"y"}', encoding="utf-8")
    (tmp_path / "a.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "c.json").write_bytes(b" " * (1024 * 1024 + 1))
    repo = ManagedGlossaryRepository(tmp_path)
    result = list_glossaries(repo)
    assert result.failure is None
    assert [item.reference.value for item in result.value] == ["a.json", "b.json", "c.json"]
    assert [item.status for item in result.value] == ["invalid", "valid", "oversized"]
    for name in ("a.json", "c.json"):
        attempt = read_glossary(repo, ref(name))
        assert attempt.failure is not None and attempt.value is None


def test_catalog_limit_before_reading_content(tmp_path):
    for n in range(1000):
        (tmp_path / f"{n:04}.json").touch()
    repo = ManagedGlossaryRepository(tmp_path)
    assert len(list_glossaries(repo).value) == 1000
    (tmp_path / "extra.json").touch()
    result = list_glossaries(repo)
    assert result.value is None
    assert result.failure == "catalog-too-large"


def test_symlink_and_hardlink_cannot_be_read_or_replaced(tmp_path):
    original = tmp_path / "original.json"
    original.write_text('{"private":"secret"}', encoding="utf-8")
    os.link(original, tmp_path / "hard.json")
    (tmp_path / "link.json").symlink_to(original)
    repo = ManagedGlossaryRepository(tmp_path)
    result = list_glossaries(repo)
    statuses = {item.reference.value: item.status for item in result.value}
    assert statuses == {"hard.json": "unsafe-multilink", "link.json": "unsafe-link", "original.json": "unsafe-multilink"}
    for name in statuses:
        assert read_glossary(repo, ref(name)).failure is not None
        assert create_glossary(repo, ref(name), (entry(),)).failure is not None
    assert original.read_text(encoding="utf-8") == '{"private":"secret"}'


def test_revision_detects_identical_external_replacement(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    result = update_glossary(repo, ref("x.json"), (entry(translation="outro"),), created.value.revision)
    assert result.failure == "stale-revision"


def test_representations_hide_content_and_identifiers(tmp_path):
    reference = ref("sensitive-name.json")
    secret = entry("sensitive-term", "sensitive-translation")
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, reference, (secret,))
    catalog = list_glossaries(repo)
    for value in (reference, secret, created, created.value, catalog, *catalog.value):
        rendered = repr(value)
        for sensitive in ("sensitive-name", "sensitive-term", "sensitive-translation", created.value.revision):
            assert sensitive not in rendered


def _create_worker(root, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = create_glossary(repo, ref("race.json"), (entry(),))
    queue.put(result.failure)


def _update_worker(root, revision, translation, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = update_glossary(repo, ref("race.json"), (entry(translation=translation),), revision)
    queue.put(result.failure)


def _run_pair(worker, arguments):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=worker, args=(*args, barrier, queue)) for args in arguments]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    return [queue.get(timeout=2) for _ in processes]


def test_simultaneous_create_and_divergent_update(tmp_path):
    failures = _run_pair(_create_worker, [(tmp_path,), (tmp_path,)])
    assert sum(failure is None for failure in failures) == 1
    snapshot = read_glossary(ManagedGlossaryRepository(tmp_path), ref("race.json")).value
    failures = _run_pair(_update_worker, [(tmp_path, snapshot.revision, "um"), (tmp_path, snapshot.revision, "dois")])
    assert failures.count(None) == 1
    assert failures.count("stale-revision") == 1


@pytest.mark.parametrize("name", ["../x.json", "/x.json", "x/y.json", "x\\y.json", ".json", "x.txt"])
def test_invalid_references_fail_closed(name):
    with pytest.raises(ValueError):
        ref(name)


@pytest.mark.parametrize("length, accepted", [(1024, True), (1025, False)])
def test_term_length_boundary(tmp_path, length, accepted):
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), (entry("x" * length),))
    assert (result.failure is None) is accepted
    assert (tmp_path / "x.json").exists() is accepted


@pytest.mark.parametrize("length, accepted", [(4096, True), (4097, False)])
def test_translation_length_boundary(tmp_path, length, accepted):
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), (entry(translation="x" * length),))
    assert (result.failure is None) is accepted
    assert (tmp_path / "x.json").exists() is accepted


@pytest.mark.parametrize("count, accepted", [(10000, True), (10001, False)])
def test_entry_count_boundary(tmp_path, count, accepted):
    entries = tuple(entry(f"x{n}", "y") for n in range(count))
    result = create_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"), entries)
    assert (result.failure is None) is accepted
    assert (tmp_path / "x.json").exists() is accepted


@pytest.mark.parametrize("size, accepted", [(1024 * 1024, True), (1024 * 1024 + 1, False)])
def test_source_file_size_boundary(tmp_path, size, accepted):
    path = tmp_path / "x.json"
    path.write_bytes(b"{}" + b" " * (size - 2))
    result = read_glossary(ManagedGlossaryRepository(tmp_path), ref("x.json"))
    assert (result.failure is None) is accepted
    if not accepted:
        assert result.value is None


def test_advanced_rules_and_unsupported_fields(tmp_path):
    path = tmp_path / "x.json"
    data = {"mage": {"rule": "translate", "translation": "mago", "required": True},
            "keep": {"rule": "preserve", "required": True}, "ban": {"rule": "forbidden"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    repo = ManagedGlossaryRepository(tmp_path)
    snapshot = read_glossary(repo, ref("x.json")).value
    result = update_glossary(repo, ref("x.json"), snapshot.entries + (entry(),), snapshot.revision)
    assert result.failure is None
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    for key in data:
        assert rewritten[key] == data[key]
    path.write_text('{"x":{"rule":"preserve","extra":"secret"}}', encoding="utf-8")
    bad = read_glossary(repo, ref("x.json"))
    assert bad.failure is not None and bad.value is None


def test_unknown_repository_exception_is_redacted():
    class Broken:
        def read(self, reference, cancel):
            raise RuntimeError("secret-term secret-path secret-revision")
    result = read_glossary(Broken(), ref("secret-name.json"))
    assert result.failure is not None
    assert result.value is None
    assert "secret" not in repr(result)
    assert "secret" not in result.failure


def test_cancel_before_create_and_update_preserves_bytes(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    cancelled = create_glossary(repo, ref("cancelled.json"), (entry(),), lambda: True)
    assert cancelled.failure == "cancelled"
    assert not (tmp_path / "cancelled.json").exists()
    created = create_glossary(repo, ref("x.json"), (entry(),))
    path = tmp_path / "x.json"
    before = path.read_bytes()
    cancelled = update_glossary(repo, ref("x.json"), (entry(translation="novo"),), created.value.revision, lambda: True)
    assert cancelled.failure == "cancelled"
    assert path.read_bytes() == before


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


def _create_alias_worker(root, name, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = create_glossary(repo, ref(name), (entry(),))
    queue.put(result.failure)


def test_concurrent_unicode_alias_creates_share_collision_domain(tmp_path):
    failures = _run_pair(_create_alias_worker, [(tmp_path, "Café.json"), (tmp_path, "CAFE\u0301.json")])
    assert sum(failure is None for failure in failures) == 1
    assert len(list_glossaries(ManagedGlossaryRepository(tmp_path)).value) == 1


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


def test_updating_keeps_a_valid_revision_for_the_next_edit(tmp_path):
    repo = ManagedGlossaryRepository(tmp_path)
    created = create_glossary(repo, ref("x.json"), (entry(),))
    first = update_glossary(repo, ref("x.json"), (entry(translation="one"),), created.value.revision)
    second = update_glossary(repo, ref("x.json"), (entry(translation="two"),), first.value.revision)
    stale = update_glossary(repo, ref("x.json"), (entry(translation="three"),), first.value.revision)

    assert first.failure is None and second.failure is None
    assert stale.failure == "stale-revision"
    assert "two" in (tmp_path / "x.json").read_text(encoding="utf-8")
