"""Cancellation, task drain and live lease authority, using controlled clocks."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from ayvu.composition.builders.rich_workers import WorkerAuthority
from ayvu.jobs import JobLeaseError
from ayvu.kernel import JobFailure, JobFailureFamily
from ayvu.translation.document_contracts import TranslationExecutionError
from ayvu.translation.project_lease import _LeaseKeeper
from ayvu.translation.project_run import _Run
from tests.support.document_workers_support import Background, Scene
from tests.support.translation_cache_v2_support import NOW


def test_cancellation_of_queued_tasks_never_opens_their_resources(tmp_path, monkeypatch):
    import ayvu.translation.document_scheduler as scheduling
    scene = Scene(tmp_path, monkeypatch, count=6, workers=4)
    scene.block_documents(monkeypatch)
    # A controlled executor leaves admitted documents queued behind the first.
    monkeypatch.setattr(scheduling, "ThreadPoolExecutor", lambda **kwargs: ThreadPoolExecutor(max_workers=1))
    task = Background(scene.run)
    try:
        assert scene.entered[1].wait(5)
        scene.source.request("user_cancelled", NOW)
        scene.release[1].set()
        task.join()
    finally:
        for event in scene.release.values():
            event.set()
        task.join()
    assert len(scene.created) == 1 and not scene.published
    assert not any(scene.entered[i].is_set() for i in range(2, 7))
    assert len(task.errors) == 1 and task.errors[0].failure.family is JobFailureFamily.CANCELLED
    assert scene.source.acknowledgement is not None


def test_cancellation_during_lookup_is_checked_before_reuse_effect(tmp_path, monkeypatch):
    from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
    scene = Scene(tmp_path, monkeypatch, count=1, workers=2)
    original, entered, release = ReadOnlyExactCache.lookup, Event(), Event()
    def lookup(reader, key):
        result = original(reader, key)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(ReadOnlyExactCache, "lookup", lookup)
    scene.env.provenance.calls.clear()
    task = Background(scene.run)
    try:
        assert entered.wait(5)
        scene.source.request("user_cancelled", NOW)
    finally:
        release.set()
        task.join()
    assert task.errors[0].failure.family is JobFailureFamily.CANCELLED
    assert not scene.env.provenance.calls and not scene.published and not scene.checkpoints


@pytest.mark.parametrize("loss", ["expired", "lost", "owner"])
def test_lease_loss_during_drain_prevents_all_effects_and_publication(tmp_path, monkeypatch, loss):
    scene = Scene(tmp_path, monkeypatch, count=2, workers=2)
    scene.block_documents(monkeypatch, failure=2)
    scene.env.provenance.calls.clear()
    task = Background(scene.run)
    try:
        assert all(event.wait(5) for event in scene.entered.values())
        scene.release[2].set()
        assert scene.finished[2].wait(5)
        assert not task.done.is_set()
        if loss == "expired":
            scene.now += timedelta(hours=1)
        elif loss == "owner":
            scene.env.job = replace(scene.env.job, lease=replace(scene.env.job.lease, owner_token="b" * 64))
        else:
            scene.lost.set()
    finally:
        scene.release[1].set()
        task.join()
    assert task.errors and not scene.published and not scene.env.provenance.calls
    assert scene.finished[1].is_set() and scene.finished[2].is_set()


def test_expired_initial_lease_refuses_before_opening_worker(tmp_path, monkeypatch):
    scene = Scene(tmp_path, monkeypatch, workers=4)
    scene.now += timedelta(hours=1)
    with pytest.raises(TranslationExecutionError, match="lease_not_held"):
        scene.run()
    assert not scene.created and not scene.published


def test_worker_uses_renewed_lease_instead_of_expired_initial_snapshot(tmp_path, monkeypatch):
    scene = Scene(tmp_path, monkeypatch, workers=2)
    scene.now += timedelta(hours=1)
    scene.env.job = replace(scene.env.job, lease=replace(scene.env.job.lease, expires_at=scene.now + timedelta(minutes=15)))
    result = scene.run()
    assert result.stats.from_cache == 4 and result.output_written


def test_acknowledged_cancellation_keeps_run_failure_precedence(tmp_path, monkeypatch):
    scene = Scene(tmp_path, monkeypatch)
    scene.source.request("user_cancelled", NOW)
    assert scene.source.token.checkpoint("pipeline_before_unit") is not None
    scene.sink._cancellation = scene.source
    cancelled = []
    scene.sink._cancel = lambda: cancelled.append(True)
    _Run._fail(scene.sink, JobFailure(JobFailureFamily.INTEGRITY, "cache_entry_corrupt"))
    assert cancelled == [True]


def test_keeper_exposes_definitive_loss_with_no_wall_clock_delay():
    renewed = Event()
    def renew():
        renewed.set()
        raise JobLeaseError("lease_not_held")
    keeper = _LeaseKeeper(renew, timedelta(seconds=30))
    class Step:
        def wait(self, interval):
            return False
        def set(self):
            pass
    keeper._stop = Step()
    keeper.start()
    assert renewed.wait(5)
    keeper.stop()
    assert keeper.lost.is_set() and not keeper._thread.is_alive()


def test_failure_of_worker_factory_stops_admission_and_joins(tmp_path, monkeypatch):
    from contextlib import contextmanager
    scene = Scene(tmp_path, monkeypatch, workers=4)
    closed = []
    @contextmanager
    def broken(events, check):
        try:
            raise ValueError("factory_failed")
            yield
        finally:
            closed.append(True)
    scene.resources.documents.factory = broken
    with pytest.raises(ValueError, match="factory_failed"):
        scene.run()
    assert closed and not scene.published


def test_run_keeps_the_single_keeper_alive_until_document_drain_finishes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    scene = Scene(tmp_path, monkeypatch, count=2, workers=2)
    scene.block_documents(monkeypatch, failure=2)
    live, stopped, outcomes = Event(), Event(), []
    run = scene.sink
    run._keeper = SimpleNamespace(start=live.set, stop=lambda: (live.clear(), stopped.set()))
    run._drive = scene.run
    run._classified = lambda error: JobFailure(JobFailureFamily.INTERNAL, "document_failed")
    run._fail = lambda failure: outcomes.append(failure)
    run._result = lambda: outcomes
    task = Background(run.execute)
    try:
        assert all(event.wait(5) for event in scene.entered.values())
        scene.release[2].set()
        assert scene.finished[2].wait(5)
        assert live.is_set() and not stopped.is_set() and not task.done.is_set()
    finally:
        scene.release[1].set()
        task.join()
    assert not task.errors and stopped.is_set() and outcomes[0].code == "document_failed"
