"""The recovery rules, over a fake of what they read. The stores are in tests/recovery/test_job_recovery_*_integration.py."""
from __future__ import annotations
import pytest
from ayvu.jobs.model import JobState
from ayvu.recovery.service import InspectJobRecoverability, RequestJobResume, ResumeRequest, ResumeStatus
from ayvu.translation.resume_reuse import ResumeStage
from ayvu.translation.project_contracts import TranslateProjectResult
from ayvu.kernel import JobFailure, JobFailureFamily
from tests.support.job_recovery_support import DURABLE, Evidence, FINGERPRINT, JOB
from tests.support.job_recovery_support import LEGACY, NEXT, OTHER_FINGERPRINT, PROJECT, Runner
from tests.support.job_recovery_support import request, snapshot, stage


def test_a_resume_needs_the_decisions_it_was_told_about():
    runner = Runner()
    result = RequestJobResume(Evidence(LEGACY)).execute(ResumeRequest("legacy:a"), runner)
    assert result.code == "decision_required" and not runner.legacy


def test_a_legacy_resume_runs_the_current_flow_and_claims_no_job():
    runner = Runner()
    result = RequestJobResume(Evidence(LEGACY)).execute(request(), runner)
    assert result.status is ResumeStatus.LEGACY_REEXECUTION and runner.legacy == ["legacy:a"] and not runner.durable
    assert result.job_id is None and result.continuation_of is None


def test_a_durable_resume_hands_over_the_revision_it_saw():
    runner = Runner()
    result = RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),)), new_id=lambda: "00000000-0000-4000-8000-00000000000a").execute(request(job_id=JOB, expected_revision=3), runner)
    assert result.status is ResumeStatus.STARTED and result.job_id == NEXT and result.continuation_of == JOB
    reference, ticket, operation = runner.durable[0]
    assert (reference, ticket.job_id, ticket.expected_revision, operation) == ("legacy:a", JOB, 3, "00000000-0000-4000-8000-00000000000a")


def test_the_idempotency_key_is_the_operation_it_runs_under():
    runner = Runner()
    RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),))).execute(request(idempotency_key="00000000-0000-4000-8000-00000000000b"), runner)
    assert runner.durable[0][2] == "00000000-0000-4000-8000-00000000000b"


@pytest.mark.parametrize("change", [dict(expected_revision=2), dict(job_id=NEXT)], ids=["revision", "job"])
def test_a_stale_request_runs_nothing(change):
    runner = Runner()
    result = RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),))).execute(request(**change), runner)
    assert result.code == "stale_revision" and not runner.durable


def test_a_request_for_a_job_already_continued_returns_the_last_of_the_chain_and_runs_nothing():
    jobs = (snapshot(state=JobState.VALIDATED, lease=False, reason="validated", job_id=NEXT, continuation_of=JOB, fingerprint=OTHER_FINGERPRINT),
            snapshot(state=JobState.FAILED, lease=False, reason="interrupted"))
    runner = Runner()
    result = RequestJobResume(Evidence(jobs=jobs)).execute(request(project_id=PROJECT, job_id=JOB), runner)
    assert result.status is ResumeStatus.ALREADY_REQUESTED and result.job_id == NEXT and result.continuation_of == JOB and not runner.durable


def test_losing_the_race_to_continue_is_the_same_answer():
    jobs = (snapshot(state=JobState.FAILED, lease=False, reason="interrupted"),)
    runner = Runner(TranslateProjectResult(PROJECT, failure=JobFailure(JobFailureFamily.POLICY, "continuation_exists")))
    result = RequestJobResume(Evidence(DURABLE, jobs=jobs)).execute(request(), runner)
    assert result.status is ResumeStatus.ALREADY_REQUESTED and result.continuation_of == JOB


def test_a_refusal_from_the_run_says_its_code():
    runner = Runner(TranslateProjectResult(PROJECT, failure=JobFailure(JobFailureFamily.POLICY, "job_in_progress")))
    result = RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),))).execute(request(), runner)
    assert result.status is ResumeStatus.REFUSED and result.code == "job_in_progress"


def test_an_active_run_is_refused_without_running():
    runner = Runner()
    result = RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),), active=True)).execute(request(), runner)
    assert result.code == "job_in_progress" and not runner.durable


def test_an_unknown_reference_is_refused():
    assert RequestJobResume(Evidence()).execute(request(), Runner()).code == "not_found"


def test_what_a_runner_raises_passes_through():
    class Interrupted:
        def run_legacy(self, reference):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        RequestJobResume(Evidence(LEGACY)).execute(request(), Interrupted())


def test_results_show_no_fingerprint_or_token():
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),))).execute("legacy:a")
    text = repr(seen) + repr(seen.entry) + repr(DURABLE) + repr(Evidence) + repr(stage(ResumeStage.TRANSLATE))
    assert FINGERPRINT not in text and "f" * 64 not in text and "same" not in text
