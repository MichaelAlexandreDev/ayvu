"""Coordinator checkpoint ownership and durable evidence independent of JSON."""
from dataclasses import replace
from threading import get_ident
from uuid import uuid4

import pytest

from ayvu.composition.builders.rich_workers import offline_work
from ayvu.jobs import InMemoryJobEventLog, JobState
from ayvu.recovery.checkpoint import ResumeStateStore, TranslationResumeState
from ayvu.translation import EngineObserver, RuntimeContext, TranslateObserver
from ayvu.translation.project_run import _Run
from tests.support.document_workers_support import Background, Scene


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_only_coordinator_saves_real_checkpoint_with_effective_workers(tmp_path, monkeypatch, workers):
    scene = Scene(tmp_path, monkeypatch, workers=workers)
    state = TranslationResumeState.create(tmp_path / "book.epub", tmp_path / "out.epub", scene.cache,
        "libretranslate", "http://localhost:5000", None, scene.runtime._options,
        False, 30, 0, execution_revision=2)
    store, writers = ResumeStateStore(tmp_path / "state"), []
    def done(index, total, name, stats):
        nonlocal state
        writers.append(get_ident())
        state = state.record_chapter(name, total, True)
        store.save(state)
    scene.sink._observer = TranslateObserver(EngineObserver(on_chapter_done=done))
    scene.block_documents(monkeypatch)
    task = Background(scene.run)
    try:
        for start in range(1, 5, workers):
            window = list(range(start, min(5, start + workers)))
            assert all(scene.entered[index].wait(5) for index in window)
            for index in reversed(window):
                scene.release[index].set()
                assert scene.finished[index].wait(5)
        task.join()
    finally:
        for event in scene.release.values():
            event.set()
        task.join()
    assert not task.errors and writers == [task.thread.ident] * 4
    saved = store.load(store.state_path_for(state))
    assert saved.version == 1 and saved.execution_revision == 2 and saved.workers == workers
    assert saved.completed_chapters == tuple(doc.logical_key for doc in scene.documents)


def test_partial_document_is_not_checkpointed_and_offline_resume_rescans_evidence(tmp_path, monkeypatch):
    import sqlite3
    from ayvu.translation.cache_only_execution import CacheOnlyDocumentExecutor
    scene = Scene(tmp_path, monkeypatch, workers=1, count=3)
    scene.env.provenance.calls.clear()
    with sqlite3.connect(scene.cache) as connection:
        connection.execute("UPDATE cache_entries_v2 SET value='broken' WHERE source_text='Document 2 unit 1'")
    with pytest.raises(Exception, match="cache_entry_corrupt"):
        scene.run()
    assert [row[1] for row in scene.checkpoints] == [1] and not scene.published
    parent = replace(scene.env.job, state=JobState.FAILED, lease=None)
    scene.env.job = replace(scene.env.job, job_id=str(uuid4()), continuation_of=parent.job_id)
    # Restoring canonical evidence is an explicit separate writer operation.
    from tests.support.rich_cache_only_support import warm_units
    scene.cache.unlink()
    warm_units(scene.cache, scene.documents)
    scene.context = replace(scene.context, job=scene.env.job, reusable_jobs=(parent.job_id,))
    scene.resources = offline_work(scene.runtime, scene.context)
    scene.checkpoints.clear()  # JSON progress is neither needed nor trusted.
    result = scene.run()
    assert result.stats.from_cache == 3 and result.output_written
    assert [row[1] for row in scene.checkpoints] == [1, 2, 3]
    assert all(call.operation == "reuse_exact_cache" for call in scene.env.provenance.calls)
    assert not scene.env.artifacts.calls


def test_continuation_chain_is_rediscovered_from_jobs_without_checkpoint(tmp_path, monkeypatch):
    from types import SimpleNamespace
    scene = Scene(tmp_path, monkeypatch)
    root = replace(scene.env.job, job_id=str(uuid4()), continuation_of=None)
    middle = replace(root, job_id=str(uuid4()), continuation_of=root.job_id)
    current = replace(root, job_id=str(uuid4()), continuation_of=middle.job_id)
    jobs = {job.job_id: job for job in (root, middle, current)}
    scene.sink._job = current
    scene.sink._ports = SimpleNamespace(jobs=SimpleNamespace(get=lambda job_id: SimpleNamespace(job=jobs[job_id])))
    # Production chains are translation_project jobs, unlike this unit fixture's original request.
    from ayvu.translation.project_contracts import TRANSLATE_COMMAND_TYPE
    for job_id, job in jobs.items():
        jobs[job_id] = replace(job, request=replace(job.request, command_type=TRANSLATE_COMMAND_TYPE))
    scene.sink._job = jobs[current.job_id]
    assert _Run._continuation_chain(scene.sink) == (middle.job_id, root.job_id)


def test_complete_durable_artifact_survives_unconsumed_result_and_is_found_by_chain(tmp_path):
    """Real project adapters: requires an environment that accepts project storage."""
    from contextlib import contextmanager
    from threading import Event
    from ayvu.composition.builders.rich_document_executor import RichDocumentExecutor
    from ayvu.jobs import CancellationSource
    from ayvu.translation import DocumentWork
    from ayvu.translation.document_contracts import SAFE_POINTS
    from ayvu.translation.engine import _Chapter
    from tests.support.translation_document_support import Env, Provider, cue, document
    env = Env(tmp_path)
    first = document(cue("a", "failed", 1), key="first.vtt")
    later = document(cue("b", "completed", 1), key="later.vtt")
    complete, release = Event(), Event()
    context = RuntimeContext(env.job, CancellationSource(SAFE_POINTS), env.log)
    @contextmanager
    def factory(events, check):
        translator = env.translator(provider=Provider())
        translator._events = events
        executor = RichDocumentExecutor(translator, context)
        execute = executor.execute_document
        def work(doc, index):
            if index == 1:
                assert complete.wait(5)
                assert release.wait(5)
                raise ValueError("first_failed")
            result = execute(doc, index)
            complete.set()
            return result
        executor.execute_document = work
        yield executor
    work = DocumentWork(factory, context)
    chapters = [_Chapter(1, "first", first, 1), _Chapter(2, "later", later, 2)]
    task = Background(lambda: list(work.scheduler.ordered(chapters, 2,
        lambda chapter, check: work.execute(chapter.document, chapter.document_index, check))))
    try:
        assert complete.wait(5)
    finally:
        release.set()
        task.join()
    assert str(task.errors[0]) == "first_failed"
    ready = env.artifacts.list_ready(env.job.job_id, limit=10)
    assert len(ready) == 1
    parent = env.job
    env.job = env.running_job()
    env.log = InMemoryJobEventLog(env.job.job_id)  # The continuation has its own event log.
    provider = Provider()
    resumed = env.translate(later, provider=provider, context=env.context(reusable_jobs=(parent.job_id,)))
    assert not provider.requests and resumed.artifact_id == ready[0].artifact_id
