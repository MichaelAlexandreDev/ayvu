"""Durable cache-only worker profiles and early governed-memory refusal."""
import pytest

from ayvu.jobs import JobState
from tests.support.rich_cache_only_support import (
    _book, _jobs, seed, invoke, config, isolate, text, offline_guard, options,
)


@pytest.mark.parametrize("arguments,workers,chunk", [
    (["--workers", "1"], 1, 3000), (["--workers", "2"], 2, 3000),
    (["--workers", "4"], 4, 3000), (["--performance-profile", "high"], 4, 4000),
    (["--performance-profile", "high", "--workers", "2"], 2, 4000),
    (["--performance-profile", "high", "--workers", "1"], 1, 4000),
])
def test_cache_only_uses_effective_workers_without_clamp(tmp_path, monkeypatch, arguments, workers, chunk):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    seed(tmp_path, source, options(workers=workers, chunk_limit=chunk), warm=False)
    selected = []
    from ayvu.composition.builders import rich_workers
    original = rich_workers.offline_work
    def resources(runtime, context):
        selected.append(runtime._options.workers)
        return original(runtime, context)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.offline_work", resources)
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, *arguments)
    assert result.exit_code == 0, text(result)
    assert selected == [workers] and (tmp_path / "out.epub").exists()
    job = _jobs(tmp_path)[0]
    assert job.request.command_version == 2 and job.state is JobState.PARTIALLY_COMPLETED
    # Cache-only never writes a compatibility checkpoint; the plan shows the effective count.
    assert any(line.split("│")[1:3] and line.split("│")[1].strip() == "Workers"
               and line.split("│")[2].strip().startswith(str(workers)) for line in text(result).splitlines())
    assert not list((tmp_path / "processing").glob("*.ayvu-state.json"))


@pytest.mark.parametrize("arguments", [["--workers", "2"], ["--workers", "4"], ["--performance-profile", "high"]])
def test_parallel_governed_memory_is_refused_before_stores_and_network(tmp_path, monkeypatch, arguments):
    from tests.support.transient_translation_support import forbidden
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    monkeypatch.setattr("ayvu.projects.store.ProjectJsonRepository.resolve", forbidden)
    with offline_guard(monkeypatch):
        result = invoke(tmp_path, source, "--translation-memory", *arguments)
    assert result.exit_code == 1 and "incompatível" in text(result)
    assert not (tmp_path / "processing").exists()
