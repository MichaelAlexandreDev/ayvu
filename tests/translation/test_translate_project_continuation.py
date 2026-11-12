"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
from dataclasses import replace
import pytest
from ayvu.jobs.model import JobState
from ayvu.translation.project_contracts import ContinuationTicket, TranslateCommandError, resolve_execution_plan as resolve_plan
from ayvu.kernel import JobFailureFamily
from tests.support.translate_project_support import Crash, _job_left_by_a_dead_process
from tests.support.translate_project_support import world


def test_a_continuation_settles_its_parent_on_another_plan_and_links_to_it(world):
    original = world.command()
    parent = _job_left_by_a_dead_process(world, original)
    resumed = world.command(overwrite=True)

    result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(parent.job_id, parent.revision))

    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED
    settled = world.jobs().get(parent.job_id).job
    assert (settled.state, settled.terminal_summary.reason_code) == (JobState.FAILED, "interrupted")
    assert world.jobs().get(result.job_id).job.continuation_of == parent.job_id


def test_a_continuation_on_the_same_plan_takes_its_one_guard_once(world):
    command = world.command(overwrite=True)
    parent = _job_left_by_a_dead_process(world, command)
    result = world.use_case(command).execute(command, continuation=ContinuationTicket(parent.job_id, parent.revision))
    assert result.state is JobState.VALIDATED and world.jobs().get(result.job_id).job.continuation_of == parent.job_id


def test_a_terminal_job_is_continued_and_keeps_its_record_byte_for_byte(world):
    first = world.run()
    before = world.jobs().get(first.job_id).job
    resumed = world.command(overwrite=True)
    result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(before.job_id, before.revision))
    assert result.state is JobState.VALIDATED and world.jobs().get(first.job_id).job == before


@pytest.mark.parametrize("change, code", [(dict(revision=+1), "stale_revision"), (dict(revision=0, again=True), "continuation_exists")])
def test_a_continuation_that_is_no_longer_right_creates_nothing(world, change, code):
    first = world.run()
    job = world.jobs().get(first.job_id).job
    resumed = world.command(overwrite=True)
    if change.get("again"):
        assert world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(job.job_id, job.revision)).state is JobState.VALIDATED
    count = len(world.all_jobs())
    result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(job.job_id, job.revision + change["revision"]))
    assert result.job_id is None and result.failure.code == code and len(world.all_jobs()) == count
    # A refusal of the request, decided before anything was settled, and not a store that failed.
    assert result.failure.family is JobFailureFamily.POLICY


def test_a_continuation_of_a_job_with_a_live_queued_lease_waits(world):
    command = world.command()
    queued = _job_left_by_a_dead_process(world, command, state=JobState.QUEUED)
    resumed = world.command(overwrite=True)
    result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(queued.job_id, queued.revision))
    assert result.job_id is None and result.failure.code == "job_not_resumable" and len(world.all_jobs()) == 1


def test_a_continuation_of_an_unknown_job_is_refused_with_its_code(world):
    command = world.command()
    result = world.use_case(command).execute(command, continuation=ContinuationTicket("00000000-0000-4000-8000-000000000009", 1))
    assert result.job_id is None and result.failure.code == "job_not_found"


def test_a_continuation_is_refused_while_the_parents_plan_is_held(world):
    original = world.command()
    parent = _job_left_by_a_dead_process(world, original)
    resumed = world.command(overwrite=True)
    fingerprint = resolve_plan(original, world.project).input_fingerprint
    with world.projects.run_guard(world.project.project_id).exclusive(fingerprint) as alone:
        assert alone
        result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(parent.job_id, parent.revision))
    assert result.failure.code == "job_in_progress" and world.jobs().get(parent.job_id).job.state is JobState.RUNNING


def test_a_crash_while_publishing_a_continuation_settles_and_can_continue_again(world):
    original = world.command()
    parent = _job_left_by_a_dead_process(world, original)
    resumed = world.command(overwrite=True)
    use_case = world.use_case(resumed)
    world.ops.before["replace"] = world.ops.crash
    world.ops.before["link"] = world.ops.crash
    with pytest.raises(Crash):
        use_case.execute(resumed, continuation=ContinuationTicket(parent.job_id, parent.revision))
    world.ops.before.clear()
    world.recover()
    jobs = world.all_jobs()
    child = next(job for job in jobs if job.continuation_of == parent.job_id)
    assert child.terminal and world.leftovers() == []
    assert not world.destination.exists()

    result = world.use_case(resumed).execute(resumed, continuation=ContinuationTicket(child.job_id, child.revision))

    assert result.state is JobState.VALIDATED and world.destination.exists()


def test_a_ticket_says_nothing_it_should_not_and_refuses_nonsense():
    assert "9" not in repr(ContinuationTicket("00000000-0000-4000-8000-000000000001", 9))
    with pytest.raises(TranslateCommandError):
        ContinuationTicket("not an id", 1)
    with pytest.raises(TranslateCommandError):
        ContinuationTicket("00000000-0000-4000-8000-000000000001", 0)
