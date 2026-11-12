"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import threading
from dataclasses import replace
import pytest
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobEventKind
from ayvu.translation.project_contracts import TRANSLATE_BEFORE_START, TranslateObserver
from ayvu.translation.engine import EngineObserver
from ayvu.providers.libretranslate.client import TranslatorError
from tests.support.epub_fakes import XHTML
from tests.support.translate_project_support import CANCEL_POINTS
from tests.support.translate_project_support import CancelAt, World
from tests.support.translate_project_support import declared_points, world


def test_the_cancel_points_under_test_are_real(world):
    known = declared_points(world)
    for point in CANCEL_POINTS:
        if point != "epub_between_members":
            assert point in known
    assert any("member" in point for point in known)


@pytest.mark.parametrize("point", CANCEL_POINTS)
def test_cancelling_at_a_safe_point_ends_cancelled_with_nothing_published(world, point):
    command = world.command()
    use_case = world.use_case(command)
    if point == "epub_between_members":
        point = next(item for item in use_case.cancellation()._safe_points if "member" in item)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), point)
    result = use_case.execute(command, cancellation=source)
    assert source.reached
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and not world.destination.exists() and world.leftovers() == []
    assert result.engine.stats.errors == []
    kinds = [event.kind for event in result.events]
    assert kinds[-3:] == [JobEventKind.CANCELLATION_REQUESTED, JobEventKind.CANCELLATION_ACKNOWLEDGED, JobEventKind.OUTCOME]
    assert source.outcome is result.outcome


def test_cancelling_keeps_the_old_file_under_overwrite(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    command = world.command(overwrite=True)
    use_case = world.use_case(command)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), "artifact_before_publication")
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and world.destination.read_bytes() == b"the old book" and world.leftovers() == []


def test_cancelling_before_the_start_never_reaches_the_translator_or_the_preflight(world):
    called = []
    command = world.command()
    use_case = world.use_case(command, lambda: called.append(1))
    source = CancelAt(tuple(use_case.cancellation()._safe_points), TRANSLATE_BEFORE_START)
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and called == [] and world.translator.calls == []


def test_cancelling_stops_the_workers_and_the_chapters_still_queued(world):
    command = world.command(workers=2)
    use_case = world.use_case(command)
    source = use_case.cancellation()
    world.translator.on_call = lambda text: source.request("user_requested", world.clock())
    result = use_case.execute(command, cancellation=source)
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and not world.destination.exists() and world.leftovers() == []
    assert len(world.translator.calls) <= 2 and result.engine.stats.errors == []


def test_cancelling_before_the_book_is_read_never_reaches_the_translator(world):
    command = world.command()
    use_case = world.use_case(command)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), "format_before_work")
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and world.translator.calls == []


def test_a_failure_after_a_worker_committed_to_cancelling_still_ends_cancelled(tmp_path):
    # A unit after "Second", so that its worker reaches a safe point once the request is in.
    world = World(tmp_path, second=XHTML.format("<p>Second</p><p>Third</p>").encode())
    command = world.command(workers=2, fail_fast=True)
    use_case = world.use_case(command)
    source = use_case.cancellation()
    committed = threading.Event()
    inner = source.token

    class Token:
        def checkpoint(self, safe_point):
            answer = inner.checkpoint(safe_point)
            if answer is not None:
                committed.set()
            return answer

    source.token = Token()

    first_is_inside = threading.Event()

    def on_call(text):
        if "Second" in text:
            assert first_is_inside.wait(timeout=30)
            source.request("user_requested", world.clock())
        elif "Hello" in text:
            first_is_inside.set()
            assert committed.wait(timeout=30)
            raise TranslatorError("the first chapter fails only now")
    world.translator.on_call = on_call
    result = use_case.execute(command, cancellation=source)
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and result.recorded


def test_ctrl_c_stops_the_chapter_another_worker_is_in(tmp_path, monkeypatch):
    released = threading.Event()

    class SpyEvent(threading.Event):
        def set(self):
            super().set()
            released.set()

    monkeypatch.setattr("ayvu.translation.engine.Event", SpyEvent)
    world = World(tmp_path, second=XHTML.format("<p>Alpha</p><p>Beta</p><p>Gamma</p>").encode())

    def on_call(text):
        if "Alpha" in text:
            assert released.wait(timeout=30)
        elif "Hello" in text:
            raise KeyboardInterrupt
    world.translator.on_call = on_call
    with pytest.raises(KeyboardInterrupt):
        world.run(workers=2)
    assert not any("Beta" in text or "Gamma" in text for text in world.translator.calls)
    assert world.all_jobs()[0].state is JobState.CANCELLED


def test_a_cancellation_that_arrives_after_the_last_safe_point_is_too_late_and_says_so(world):
    command = world.command()
    use_case = world.use_case(command)
    source = use_case.cancellation()
    answers = []
    def on_event(event):
        payload = event.payload
        if getattr(payload, "phase", None) == "publishing" and payload.completed == 1:
            answers.append(source.request("user_requested", world.clock()).status.value)
    result = use_case.execute(command, cancellation=source, observer=TranslateObserver(on_event=on_event))
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and answers == ["accepted"]
    assert source.request("again", world.clock()).status.value == "too_late"


def test_a_consumer_that_fails_cannot_change_how_the_job_ends(world):
    def on_event(event):
        raise RuntimeError("the window was closed")
    result = world.run(observer=TranslateObserver(on_event=on_event))
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED


def test_an_engine_observer_that_fails_ends_the_job_as_a_failure(world):
    def on_chapter_done(*_):
        raise RuntimeError("could not save the resume file")
    result = world.run(observer=TranslateObserver(engine=EngineObserver(on_chapter_done=on_chapter_done)))
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and isinstance(result.raised, RuntimeError)


@pytest.mark.parametrize("workers", [1, 2])
def test_ctrl_c_during_translation_ends_cancelled_and_reaches_the_caller(world, workers):
    def interrupt(text):
        raise KeyboardInterrupt
    world.translator.on_call = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(workers=workers)
    job = world.all_jobs()[0]
    assert job.state is JobState.CANCELLED and job.lease is None
    assert not world.destination.exists() and world.leftovers() == []


def test_ctrl_c_during_the_preflight_ends_failed(world):
    def prepare():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(prepare=prepare)
    job = world.all_jobs()[0]
    assert job.state is JobState.FAILED and job.terminal_summary.reason_code == "interrupted"


def test_ctrl_c_after_the_file_became_visible_says_the_file_is_there(world):
    def interrupt(*_):
        raise KeyboardInterrupt
    world.ops.after["link"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run()
    job = world.all_jobs()[0]
    assert job.state is JobState.PARTIALLY_COMPLETED and job.terminal_summary.reason_code == "interrupted_after_publication"
    assert world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(job) == [("ready", None)]


def test_ctrl_c_before_the_file_became_visible_leaves_the_old_file_alone(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    def interrupt(*_):
        raise KeyboardInterrupt
    world.ops.before["replace"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    assert job.state is JobState.CANCELLED
    assert world.destination.read_bytes() == b"the old book" and world.leftovers() == []
