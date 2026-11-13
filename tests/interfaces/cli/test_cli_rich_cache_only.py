"""Durable labeled cache-only CLI: real stores, publication and forbidden egress."""
from dataclasses import replace
import sqlite3
from zipfile import ZipFile

import pytest

from ayvu.knowledge.cache.repository import SQLiteExactCache
from tests.support.rich_cache_only_support import (
    _book, _jobs, seed, invoke, config, isolate, text, records, offline_guard, options,
)
from tests.support.transient_translation_support import exact_entry
from tests.knowledge.cache.read_only_support import snapshot


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_coverage_gate_and_durable_terminal_state(tmp_path, monkeypatch, warm, full):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, cache, _ = seed(tmp_path, source, options(), warm=warm)
    original = source.read_bytes(), source.stat().st_mode
    previous = tmp_path / "out.epub"
    previous.write_bytes(b"previous output")
    previous.chmod(0o640)
    old_output = previous.read_bytes(), previous.stat().st_mode
    cache_before = snapshot(tmp_path).get(cache.name)
    if full and not warm:
        from ayvu.formats import FormatService
        monkeypatch.setattr(FormatService, "reconstruct", lambda *a, **k: pytest.fail("full-cache reconstructed"))
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--overwrite", *(["--require-full-cache"] if full else []))
    assert result.exit_code == int(full and not warm), text(result)
    job = _jobs(tmp_path)[0]
    assert job.request.command_version == 2
    reason = job.terminal_summary.reason_code
    assert reason == ("validated" if warm else "cache_incomplete" if full else "texts_missing")
    assert (source.read_bytes(), source.stat().st_mode) == original
    if cache_before is not None:
        assert snapshot(tmp_path)[cache.name] == cache_before
    else:
        assert not cache.exists()
    missing = list((tmp_path / "books").rglob("*-missing*.txt"))
    assert len(missing) == int(not warm)
    if full and not warm:
        assert (previous.read_bytes(), previous.stat().st_mode) == old_output
    else:
        with ZipFile(previous) as archive:
            body = archive.read("OEBPS/c1.xhtml").decode()
            assert ("PT:Hello reader." in body) is warm
    calls = records(tmp_path, job)
    assert not any(call.operation in {"translate", "store_raw_results", "replay_raw_results"} for call in calls)
    reused = [call for call in calls if call.operation == "reuse_exact_cache"]
    assert len(reused) == (5 if warm else 0)
    assert all(call.configuration_refs[0].kind == "exact_cache" for call in reused)
    assert not list((tmp_path / "processing").rglob("*raw-results*"))


@pytest.mark.parametrize("state", ["ambiguous", "model", "chunk_limit", "revision"])
def test_local_identity_incompatibility_keeps_source_and_missing_output(tmp_path, monkeypatch, state):
    from tests.support.translation_cache_v2_support import make_plan
    from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
    from ayvu.knowledge.cache import OpaqueSemanticRevision
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, cache, documents = seed(tmp_path, source, options(), warm=False)
    producer = replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026")
    plan = make_plan(producer=producer)
    if state == "model":
        plan = replace(plan, producer=replace(producer, model="declared:other"), route_hops=None)
    if state == "chunk_limit":
        plan = replace(plan, chunk_limit=4000)
    if state == "revision":
        plan = replace(plan, normalization_revision=OpaqueSemanticRevision("translation", "normalization", 1, "changed"))
    writer = SQLiteExactCache(cache)
    source_text = documents[0].units[0].source_text
    writer.put_if_absent(exact_entry(project, plan, source_text, "Cached"))
    if state == "ambiguous":
        other = replace(plan, capabilities="capfp:v1:" + "c" * 64, route_hops=None)
        writer.put_if_absent(exact_entry(project, other, source_text, "Other"))
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source)
    assert result.exit_code == 0, text(result)
    missing = next((tmp_path / "books").rglob("*-missing.txt"))
    assert "# Missing texts: 5" in missing.read_text()


@pytest.mark.parametrize("problem", ["corrupt", "storage", "overflow"])
def test_fatal_cache_after_previous_hits_preserves_every_existing_file(tmp_path, monkeypatch, problem):
    from tests.support.translation_cache_v2_support import make_plan
    from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, cache, documents = seed(tmp_path, source, options())
    if problem == "corrupt":
        with sqlite3.connect(cache) as conn:
            conn.execute("UPDATE cache_entries_v2 SET value='broken' WHERE source_text=?", (documents[-1].units[-1].source_text,))
    elif problem == "storage":
        cache.with_name(cache.name + "-wal").write_bytes(b"unsafe storage")
    else:
        producer = replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026")
        writer = SQLiteExactCache(cache)
        for index in range(257):
            plan = make_plan(producer=producer, capabilities="capfp:v1:" + f"{index:064x}")
            writer.put_if_absent(exact_entry(project, plan, documents[-1].units[-1].source_text, "Other"))
    output = tmp_path / "out.epub"
    output.write_bytes(b"previous")
    output.chmod(0o640)
    before = {path: (path.read_bytes(), path.stat().st_mode) for path in (source, cache, output)}
    from ayvu.formats import FormatService
    monkeypatch.setattr(FormatService, "reconstruct", lambda *a, **k: pytest.fail("fatal cache reconstructed"))
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--overwrite")
    assert result.exit_code == 1, text(result)
    assert ("cache_entry_corrupt" if problem == "corrupt" else "cache_recovery_required" if problem == "storage"
            else "cache_candidate_limit_exceeded") in text(result)
    assert all((path.read_bytes(), path.stat().st_mode) == old for path, old in before.items())
    assert not list((tmp_path / "books").rglob("*.txt"))


def test_hot_cache_cannot_replay_or_publish_new_raw_results(tmp_path, monkeypatch):
    from tests.support.cli_rich_runtime_support import invoke as online
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    warmed, transport = online(tmp_path, monkeypatch, source, "--translation-memory")
    assert warmed.exit_code == 0 and transport.posts, text(warmed)
    raw_before = {p: p.read_bytes() for p in (tmp_path / "processing").rglob("*") if p.is_file() and "artifacts" in p.parts}
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--overwrite", "--require-full-cache", "--translation-memory")
    assert result.exit_code == 0, text(result)
    raw_after = {p: p.read_bytes() for p in (tmp_path / "processing").rglob("*") if p.is_file() and "artifacts" in p.parts}
    new_files = raw_after.keys() - raw_before.keys()
    assert all(p.suffix != ".json" for p in new_files)
    assert all(raw_after[p] == data for p, data in raw_before.items())
    job = _jobs(tmp_path)[-1]
    assert not any(r.operation in {"store_raw_results", "replay_raw_results", "translate"} for r in records(tmp_path, job))
