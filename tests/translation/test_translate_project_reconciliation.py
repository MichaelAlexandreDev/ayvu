"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
import pytest
from ayvu.jobs.model import JobState
from tests.support.translate_project_support import Crash, _job_left_by_a_dead_process
from tests.support.translate_project_support import _job_request, world


def test_a_crash_while_writing_is_settled_as_failed_and_the_temporary_file_goes(world):
    world.ops.after["write"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    assert [path.name for path in world.destination.parent.iterdir() if path.name.endswith(".part")]
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING
    assert world.recover() == (job.job_id,)
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.FAILED and job.terminal_summary.reason_code == "interrupted"
    assert not world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(job) == [("abandoned", "interrupted_before_digest")]


def test_a_crash_after_staging_never_touches_the_old_file(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.ops.before["replace"] = world.ops.crash
    with pytest.raises(Crash):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    world.recover(world.command(overwrite=True))
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.FAILED and world.destination.read_bytes() == b"the old book" and world.leftovers() == []
    assert world.artifacts_of(job) == [("abandoned", "final_mismatch")]


def test_a_crash_right_after_the_link_finishes_the_publication_on_recovery(world):
    world.ops.after["link"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    assert world.destination.exists() and world.destination.stat().st_nlink == 2
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING and world.artifacts_of(job) == [("staging", None)]
    assert world.recover() == (job.job_id,)
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and job.terminal_summary.reason_code == "interrupted_after_publication"
    assert world.artifacts_of(job) == [("ready", None)]
    assert world.destination.stat().st_nlink == 1 and world.leftovers() == []
    records = world.records(job.job_id)
    assert len(records) == 1 and len(records[0].artifacts) == 1 and records[0].outcome_code == "interrupted_after_publication"


def test_a_crash_right_after_the_swap_finishes_the_publication_on_recovery(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.ops.after["replace"] = world.ops.crash
    with pytest.raises(Crash):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    world.recover(world.command(overwrite=True))
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and world.artifacts_of(job) == [("ready", None)]
    assert world.destination.read_bytes() != b"the old book" and world.leftovers() == []


def test_a_crash_after_the_record_went_ready_still_ends_the_job(world):
    command = world.command()
    use_case = world.use_case(command)
    original = use_case._ports.jobs.transition
    def transition(job_id, target, **kwargs):
        if target is JobState.COMPLETED:
            raise Crash
        return original(job_id, target, **kwargs)
    use_case._ports.jobs.transition = transition
    with pytest.raises(Crash):
        use_case.execute(command)
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING and world.artifacts_of(job) == [("ready", None)]
    world.recover()
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and world.leftovers() == []


@pytest.mark.parametrize("stop_at, expected, reason", [
    (JobState.QUEUED, JobState.CANCELLED, "interrupted"),
    (JobState.PREFLIGHT, JobState.FAILED, "interrupted"),
    (JobState.RUNNING, JobState.FAILED, "interrupted"),
])
def test_a_job_left_behind_in_each_early_state_gets_one_terminal_outcome(world, stop_at, expected, reason):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    job = jobs.create(_job_request(world, command))
    job = jobs.acquire_lease(job.job_id, timedelta(minutes=30))
    for state in {JobState.QUEUED: (), JobState.PREFLIGHT: (JobState.PREFLIGHT,), JobState.RUNNING: (JobState.PREFLIGHT, JobState.RUNNING)}[stop_at]:
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    assert world.recover() == (job.job_id,)
    settled = world.jobs().get(job.job_id).job
    assert settled.state is expected and settled.terminal_summary.reason_code == reason
    assert world.recover() == ()


@pytest.mark.parametrize("steps, expected", [
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING), JobState.CANCELLED),
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED), JobState.PARTIALLY_COMPLETED),
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING), JobState.PARTIALLY_COMPLETED),
])
def test_a_job_left_behind_in_each_late_state_gets_one_terminal_outcome(world, steps, expected):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    job = jobs.acquire_lease(jobs.create(_job_request(world, command)).job_id, timedelta(minutes=30))
    for state in steps:
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    world.recover()
    assert world.jobs().get(job.job_id).job.state is expected


def test_a_job_with_a_live_lease_is_left_alone_and_a_duplicate_is_refused(world):
    command = world.command()
    outcomes = []
    def second_run(text):
        if not outcomes:
            outcomes.append(None)
            outcomes[0] = world.use_case(command).execute(command)
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    duplicate = outcomes[0]
    assert duplicate.job_id is None and duplicate.failure.code == "job_in_progress"
    assert first.state is JobState.VALIDATED
    assert len(world.all_jobs()) == 1


def test_a_live_lease_on_this_plan_with_no_live_owner_is_taken_over(world):
    command = world.command()
    left = _job_left_by_a_dead_process(world, command)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    settled = world.jobs().get(left.job_id).job
    assert settled.state is JobState.FAILED and settled.terminal_summary.reason_code == "interrupted"


def test_a_live_lease_on_another_plan_is_left_alone(world):
    command = world.command()
    other = _job_left_by_a_dead_process(world, command, fingerprint="sha256:" + "a" * 64)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    assert world.jobs().get(other.job_id).job.state is JobState.RUNNING


def test_a_queued_job_keeps_its_lease_until_it_expires(world):
    command = world.command()
    queued = _job_left_by_a_dead_process(world, command, state=JobState.QUEUED)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    assert world.jobs().get(queued.job_id).job.state is JobState.QUEUED
    world.clock.advance(hours=2)
    assert world.use_case(command).reconcile(command.project_id) == (queued.job_id,)


def test_the_guard_is_given_back_after_ctrl_c(world):
    command = world.command()
    def interrupt(_text):
        raise KeyboardInterrupt
    world.translator.on_call = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.use_case(command).execute(command)
    world.translator.on_call = None

    assert world.use_case(command).execute(command).state is JobState.VALIDATED


def test_a_guard_that_cannot_be_taken_refuses_before_any_job(world):
    command = world.command()
    use_case = world.use_case(command)
    root = world.processing / "projects" / world.project.project_id / "runs"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(b"blocked")

    result = use_case.execute(command)

    assert result.job_id is None and result.failure.code == "run_guard_unavailable"
    assert world.all_jobs() == []


def test_a_job_left_behind_is_settled_before_new_work_starts(world):
    world.ops.after["write"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    job = world.all_jobs()[0]
    world.ops.after.clear()
    world.ops.dead = False
    world.clock.advance(hours=2)
    result = world.run()
    assert result.state is JobState.VALIDATED and result.job_id != job.job_id
    assert world.jobs().get(job.job_id).job.state is JobState.FAILED and world.leftovers() == []


def test_a_job_that_gave_up_its_lease_before_ending_is_settled_too(world):
    command = world.command()
    jobs = world.use_case(command)._ports.jobs
    job = jobs.acquire_lease(jobs.create(_job_request(world, command)).job_id, timedelta(minutes=30))
    for state in (JobState.PREFLIGHT, JobState.RUNNING):
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    jobs.release_lease(job.job_id, job.lease.owner_token)
    assert world.use_case(command).reconcile(world.project.project_id) == (job.job_id,)
    assert world.jobs().get(job.job_id).job.state is JobState.FAILED


def test_a_job_another_process_moved_is_reported_as_the_record_says(world):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    moved = []

    def on_call(text):
        if not moved:
            job = world.all_jobs()[0]
            moved.append(jobs.transition(job.job_id, JobState.CANCELLING, expected_revision=job.revision))
    world.translator.on_call = on_call
    result = use_case.execute(command)
    assert result.state is JobState.CANCELLING and not result.recorded and not result.output_ready and result.artifact is None


def test_progress_can_never_crowd_out_the_terminal_outcome(world, monkeypatch):
    monkeypatch.setattr("ayvu.translation.project_run.DEFAULT_EVENT_LOG_CAPACITY", 8)
    result = world.run()
    world.assert_consistent(result)
    assert len(result.events) <= 8 and result.events[-1].payload == result.outcome


def test_a_finished_job_does_not_stop_the_same_work_being_asked_for_again(world):
    first = world.run()
    world.destination.unlink()
    second = world.run()
    assert first.state is JobState.VALIDATED and second.state is JobState.VALIDATED and first.job_id != second.job_id
    assert world.translator.calls and len(set(world.translator.calls)) == len(world.translator.calls)
