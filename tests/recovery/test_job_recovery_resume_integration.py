"""Recovery over the production stores, the real state files and the real CLI, under a fake translator.

A "crash" here is an exception nothing in production catches, raised from the translator. It leaves a job running
with a live lease and a guard nobody holds, as a killed process does. A real kill is in the concurrency tests.
"""
from __future__ import annotations
from datetime import timedelta
import pytest
from ayvu.jobs.model import JobRequest, JobState
from ayvu.recovery.service import CompatibilityStatus, RequiredDecision, ResumeRequest, ResumeStatus
from ayvu.translation.resume_reuse import RecoveryReason, ResumeStage
from ayvu.translation.project_contracts import OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, ContinuationTicket, TranslateProjectCommand
from ayvu.composition.builders.translate_project import open_translate_project
from tests.support.cli_translate_project_support import ENCRYPTED_DOCUMENT, _book
from ayvu.kernel import OutputPlan
from tests.support.job_recovery_integration_support import DECIDED, Runner, Scene
from tests.support.job_recovery_integration_support import _isolated_from_the_user
from tests.support.job_recovery_integration_support import scene, tree


def test_a_killed_run_resumes_as_a_linked_job_and_repeats_only_what_is_missing(scene):
    state_file = scene.interrupted_state()
    before = state_file.read_bytes()
    scene.translator.calls.clear()

    result = scene.request()

    assert result.status is ResumeStatus.STARTED and result.result.state is JobState.VALIDATED
    assert scene.translator.calls == ["Goodbye reader."]
    parent, child = scene.jobs()
    assert (parent.state, parent.terminal_summary.reason_code) == (JobState.FAILED, "interrupted")
    assert child.continuation_of == parent.job_id and child.state is JobState.VALIDATED
    assert scene.output.exists() and state_file.read_bytes() == before


def test_the_repeated_call_is_disclosed_and_the_cache_is_not_called_proof(scene):
    scene.interrupted_state()
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert seen.provider_calls_may_repeat and RequiredDecision.FULL_REEXECUTION in seen.required_decisions
    assert seen.selection.resume_from is ResumeStage.TRANSLATE and seen.selection.reusable == ()
    assert RecoveryReason.PROVENANCE_UNPROVEN in seen.entry.reasons


def test_a_finished_run_is_not_proved_reusable_either(scene):
    scene.save_state()
    assert scene.translate().result.state is JobState.VALIDATED
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert seen.selection.resume_from is ResumeStage.TRANSLATE and seen.selection.reason is RecoveryReason.ARTIFACT_MISSING
    assert RecoveryReason.PROVENANCE_UNPROVEN in seen.entry.reasons
    assert RecoveryReason.TERMINAL_JOB in seen.entry.reasons


def test_ctrl_c_resumes_as_a_linked_job_without_reopening_the_cancelled_one(scene):
    scene.save_state()
    def stop(text):
        if "Goodbye" in text:
            scene.translator.on_call = None
            raise KeyboardInterrupt
    scene.translator.on_call = stop
    with pytest.raises(KeyboardInterrupt):
        scene.translate()
    cancelled = scene.jobs()[0]
    assert cancelled.state is JobState.CANCELLED

    result = scene.request()

    assert result.result.state is JobState.VALIDATED
    assert scene.jobs()[0].state is JobState.CANCELLED and scene.jobs()[0].revision == cancelled.revision
    assert scene.jobs()[1].continuation_of == cancelled.job_id


def test_an_expired_lease_is_reconciled_and_continued(scene):
    scene.interrupted_state()
    scene.clock.ahead = timedelta(hours=2)
    entry = scene.only_entry()
    assert RecoveryReason.EXPIRED_LEASE in entry.reasons and entry.resumable
    assert scene.request().result.state is JobState.VALIDATED
    assert scene.jobs()[0].terminal_summary.reason_code == "interrupted"


def test_a_resume_of_a_resume_takes_each_guard_once(scene):
    """The continuation runs with overwrite on, as does a resume of it, so parent and child share a plan."""
    scene.interrupted_state()
    ticket_request = scene.only_entry()
    scene.crash(overwrite=True, continuation=ContinuationTicket(ticket_request.job_id, ticket_request.expected_revision), on="Goodbye")
    parent, middle = scene.jobs()
    assert parent.request.input_fingerprint != middle.request.input_fingerprint

    result = scene.request()

    assert result.status is ResumeStatus.STARTED and result.result.state is JobState.VALIDATED
    chain = scene.jobs()
    assert [job.continuation_of for job in chain] == [None, parent.job_id, middle.job_id]
    assert chain[1].request.input_fingerprint == chain[2].request.input_fingerprint


def test_a_live_run_is_seen_as_active_and_blocks_a_resume(scene):
    scene.save_state()
    outcome = {}

    def while_running(text):
        if "Goodbye" in text and not outcome:
            recovery = scene.recovery()
            entry = recovery.list_interrupted.execute().entries[0]
            outcome["entry"] = entry
            outcome["resume"] = scene.request(recovery=recovery)
    scene.translator.on_call = while_running

    finished = scene.translate()

    entry, resume = outcome["entry"], outcome["resume"]
    assert RecoveryReason.ACTIVE_LEASE in entry.reasons and not entry.resumable
    assert resume.status is ResumeStatus.REFUSED and resume.code == "job_in_progress"
    assert finished.result.state is JobState.VALIDATED and len(scene.jobs()) == 1


def test_a_resume_racing_the_run_is_refused_by_the_guard_itself(scene):
    """Between the look and the run the original comes back: the parent's guard is what stops it."""
    scene.interrupted_state()
    entry = scene.only_entry()
    project = scene.project()
    guard = scene.projects.run_guard(project.project_id)
    with guard.exclusive(scene.jobs()[0].request.input_fingerprint) as alone:
        assert alone
        run = scene.translate(overwrite=True, continuation=ContinuationTicket(entry.job_id, entry.expected_revision))
    assert run.result.failure.code == "job_in_progress" and run.result.job_id is None
    assert [job.state for job in scene.jobs()] == [JobState.RUNNING]


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_a_source_that_is_not_as_recorded_refuses_and_leaves_everything(scene, change):
    state_file = scene.interrupted_state()
    state_before = state_file.read_bytes()
    if change == "changed":
        _book(scene.source, chapter_two="<p>Goodbye reader, changed.</p>")
    else:
        scene.source.unlink()
    recovery = scene.recovery()
    entry = recovery.list_interrupted.execute().entries[0]
    seen = recovery.inspect.execute(entry.reference)
    assert not seen.entry.resumable
    assert (RecoveryReason.SOURCE_CHANGED if change == "changed" else RecoveryReason.SOURCE_MISSING) in seen.entry.reasons

    result = recovery.request_resume.execute(ResumeRequest(entry.reference, accepted=DECIDED), Runner(scene))

    assert result.status is ResumeStatus.REFUSED and result.code == f"source_{change}"
    assert state_file.read_bytes() == state_before and not scene.output.exists()


def test_policy_refuses_before_the_parent_is_touched(scene):
    state_file = scene.interrupted_state()
    state_before = state_file.read_bytes()
    jobs_before = scene.jobs()
    recovery = scene.recovery(policy_refusal=lambda _path: "encrypted_content")

    result = scene.request(recovery=recovery)

    assert result.status is ResumeStatus.REFUSED and result.code == "policy_denied"
    assert scene.jobs() == jobs_before and state_file.read_bytes() == state_before and not scene.output.exists()


def test_the_real_policy_check_sees_a_protected_book_on_the_legacy_path(tmp_path):
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))
    scene = Scene(tmp_path, source=source)
    scene.save_state()
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert RecoveryReason.POLICY_DENIED in seen.entry.reasons


def test_a_job_with_no_file_cannot_be_resumed_and_says_why(scene):
    state_file = scene.interrupted_state()
    state_file.unlink()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.JOB_ONLY and RecoveryReason.INVOCATION_UNAVAILABLE in entry.reasons
    result = scene.request(reference=entry.reference)
    assert result.status is ResumeStatus.REFUSED and result.code == "invocation_unavailable" and len(scene.jobs()) == 1


def test_a_request_repeated_after_finishing_runs_nothing_and_names_the_job(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    first = scene.request()
    calls = list(scene.translator.calls)

    again = scene.recovery().request_resume.execute(
        ResumeRequest(entry.reference, entry.job_id, entry.project_id, entry.expected_revision, DECIDED), Runner(scene))

    assert again.status is ResumeStatus.ALREADY_REQUESTED and again.job_id == first.job_id
    assert scene.translator.calls == calls and len(scene.jobs()) == 2


def test_a_stale_ticket_creates_nothing(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    run = scene.translate(overwrite=True, continuation=ContinuationTicket(entry.job_id, entry.expected_revision + 1))
    assert run.result.failure.code == "stale_revision" and len(scene.jobs()) == 1


def test_a_continuation_left_queued_by_a_crash_does_not_block_the_next_resume(scene):
    scene.interrupted_state()
    parent = scene.jobs()[0]
    scene.request()  # A complete chain first, so that the queued one is on the same plan as what follows.
    repository = scene.jobs_repository()
    chain_end = repository.list_jobs(parent.request.project_id, limit=8, newest_first=True)[0].job
    stuck = repository.create_continuation(chain_end.job_id, JobRequest(
        parent.request.project_id, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, chain_end.request.input_fingerprint, (OUTPUT_ROLE,)))
    scene.save_state()

    result = scene.request()

    assert result.result.state is JobState.VALIDATED
    settled = repository.get(stuck.job_id).job
    assert (settled.state, settled.terminal_summary.reason_code) == (JobState.CANCELLED, "interrupted")
    assert scene.jobs()[-1].continuation_of == stuck.job_id


def test_cancelling_a_resumed_run_leaves_a_truthful_state_that_can_resume_again(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    project = scene.project()
    runtime = scene.runtime()
    use_case = open_translate_project(scene.processing, project.project_id, scene.source, scene.output, overwrite=True, runtime=runtime, clock=scene.clock)
    cancellation = use_case.cancellation()
    cancellation.request("user_requested", scene.clock())
    result = use_case.execute(TranslateProjectCommand(project.project_id, project.source_sha256, scene.options, OutputPlan(path=scene.output.resolve()), overwrite=True),
                              cancellation=cancellation, continuation=ContinuationTicket(entry.job_id, entry.expected_revision))
    assert result.state is JobState.CANCELLED
    jobs = scene.jobs()
    assert [job.state for job in jobs] == [JobState.FAILED, JobState.CANCELLED] and jobs[1].continuation_of == jobs[0].job_id

    assert scene.request().result.state is JobState.VALIDATED
    assert scene.jobs()[2].continuation_of == jobs[1].job_id


def test_cancelling_a_long_read_writes_nothing_and_decides_nothing(scene):
    scene.interrupted_state()
    before = tree(scene.tmp_path)
    recovery = scene.recovery()
    entry = recovery.list_interrupted.execute().entries[0]
    cancellation = recovery.inspect.cancellation()
    cancellation.request("user_requested", scene.clock())

    seen = recovery.inspect.execute(entry.reference, cancellation=cancellation)

    assert seen.cancelled and seen.required_decisions == () and tree(scene.tmp_path) == before
