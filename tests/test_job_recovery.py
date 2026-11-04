"""The recovery rules, over a fake of what they read. The stores are in test_job_recovery_integration.py."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ayvu.job import JobLease, JobRequest, JobState, JobTerminalSummary, TranslationJob
from ayvu.job_events import JobFailure, JobFailureFamily
from ayvu.job_recovery import (
    REUSE_DIMENSIONS, ArtifactIntegrity, CompatibilityStatus, InspectJobRecoverability, InvocationView, ListInterruptedJobs, OutputEvidence,
    RecoveryCancelled, RecoveryReason, RecoveryScan, RequestJobResume, RequiredDecision, ResumeRequest, ResumeStage, ResumeStatus, SourceState,
    StageEvidence, classify_job, select_resume_stage,
)
from ayvu.job_service import JobSnapshot
from ayvu.translate_project import TranslateProjectResult

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PROJECT = "00000000-0000-4000-8000-000000000001"
JOB = "00000000-0000-4000-8000-000000000002"
NEXT = "00000000-0000-4000-8000-000000000003"
FINGERPRINT = "sha256:" + "a" * 64
OTHER_FINGERPRINT = "sha256:" + "b" * 64
ALL_EQUAL = tuple((name, "same") for name in REUSE_DIMENSIONS)


def job(state=JobState.RUNNING, *, job_id=JOB, lease=True, expired=False, reason=None, revision=3, continuation_of=None, fingerprint=FINGERPRINT):
    held = JobLease("f" * 64, NOW - timedelta(hours=2), NOW - timedelta(hours=1) if expired else NOW + timedelta(hours=1)) if lease else None
    summary = JobTerminalSummary(reason) if reason else None
    return TranslationJob(job_id, JobRequest(PROJECT, "translate_project", 1, fingerprint, ("translated_output",)), state, revision,
                          NOW, NOW, held, summary, continuation_of)


def snapshot(*args, **kwargs):
    made = job(*args, **kwargs)
    return JobSnapshot(made, not made.terminal and made.lease is not None and made.lease.expired(NOW))


def stage(stage_id, integrity=ArtifactIntegrity.VERIFIED, **changes):
    return StageEvidence(stage_id, integrity, **{"expected": ALL_EQUAL, "produced": ALL_EQUAL, **changes})


@pytest.mark.parametrize("name", REUSE_DIMENSIONS)
def test_a_change_in_any_dimension_prevents_reuse_and_is_named(name):
    produced = tuple((item, "other" if item == name else "same") for item in REUSE_DIMENSIONS)
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE, produced=produced), stage(ResumeStage.RECONSTRUCT)))
    assert selected.resume_from is ResumeStage.TRANSLATE and selected.reason is RecoveryReason.INCOMPATIBLE_PLAN
    assert selected.changed == (name,) and selected.reusable == ()


@pytest.mark.parametrize("name", REUSE_DIMENSIONS)
def test_a_dimension_nobody_recorded_proves_nothing_and_is_not_called_changed(name):
    produced = tuple((item, None if item == name else "same") for item in REUSE_DIMENSIONS)
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE, produced=produced), stage(ResumeStage.RECONSTRUCT)))
    assert selected.reason is RecoveryReason.PROVENANCE_UNPROVEN and selected.not_provable == (name,) and selected.changed == ()


@pytest.mark.parametrize("integrity, reason", [
    (ArtifactIntegrity.MISSING, RecoveryReason.ARTIFACT_MISSING), (ArtifactIntegrity.PARTIAL, RecoveryReason.ARTIFACT_PARTIAL),
    (ArtifactIntegrity.CORRUPT, RecoveryReason.ARTIFACT_CORRUPT), (ArtifactIntegrity.NOT_READY, RecoveryReason.ARTIFACT_MISSING),
])
def test_an_artifact_that_is_not_intact_selects_its_own_stage(integrity, reason):
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE), stage(ResumeStage.RECONSTRUCT, integrity)))
    assert selected.resume_from is ResumeStage.RECONSTRUCT and selected.reason is reason and selected.reusable == (ResumeStage.TRANSLATE,)


def test_a_valid_raw_stage_is_reusable_and_only_downstream_work_remains():
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE), stage(ResumeStage.RECONSTRUCT, ArtifactIntegrity.MISSING)))
    assert selected.reusable == (ResumeStage.TRANSLATE,) and selected.resume_from is ResumeStage.RECONSTRUCT


def test_only_complete_evidence_proves_every_stage():
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE), stage(ResumeStage.RECONSTRUCT)))
    assert selected.resume_from is None and selected.reason is None and selected.reusable == (ResumeStage.TRANSLATE, ResumeStage.RECONSTRUCT)


@pytest.mark.parametrize("change", [{"owned": False}, {"schema_valid": False}], ids=["other_owner", "invalid_schema"])
def test_a_verified_artifact_of_another_owner_or_schema_is_not_reused(change):
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE, **change), stage(ResumeStage.RECONSTRUCT)))
    assert selected.resume_from is ResumeStage.TRANSLATE and selected.reusable == ()


def test_a_ready_record_whose_bytes_nobody_read_is_never_reused():
    selected = select_resume_stage((stage(ResumeStage.TRANSLATE, ArtifactIntegrity.UNCHECKED), stage(ResumeStage.RECONSTRUCT)))
    assert selected.resume_from is ResumeStage.TRANSLATE and selected.reason is RecoveryReason.PROVENANCE_UNPROVEN


def test_no_evidence_starts_from_the_beginning():
    selected = select_resume_stage(())
    assert selected.resume_from is ResumeStage.TRANSLATE and selected.reason is RecoveryReason.ARTIFACT_MISSING


@pytest.mark.parametrize("made, active, reasons, resumable", [
    (dict(lease=True), False, (RecoveryReason.LEASE_OWNER_GONE, RecoveryReason.RECOVERABLE_INTERRUPTION), True),
    (dict(lease=True), True, (RecoveryReason.ACTIVE_LEASE,), False),
    (dict(expired=True), False, (RecoveryReason.EXPIRED_LEASE, RecoveryReason.RECOVERABLE_INTERRUPTION), True),
    (dict(expired=True), True, (RecoveryReason.ACTIVE_LEASE,), False),
    (dict(lease=False), False, (RecoveryReason.RECOVERABLE_INTERRUPTION,), True),
    (dict(state=JobState.QUEUED, lease=False), False, (RecoveryReason.RECOVERABLE_INTERRUPTION,), True),
    (dict(state=JobState.QUEUED, lease=True), False, (RecoveryReason.ACTIVE_LEASE,), False),
    (dict(state=JobState.PAUSED, lease=False), False, (RecoveryReason.JOB_STATE_NOT_RESUMABLE,), False),
    (dict(state=JobState.PAUSING, lease=True), False, (RecoveryReason.JOB_STATE_NOT_RESUMABLE,), False),
    (dict(state=JobState.CANCELLED, lease=False, reason="cancelled"), False, (RecoveryReason.TERMINAL_JOB, RecoveryReason.RECOVERABLE_INTERRUPTION), True),
    (dict(state=JobState.FAILED, lease=False, reason="provider_failed"), False, (RecoveryReason.TERMINAL_JOB, RecoveryReason.RECOVERABLE_INTERRUPTION), True),
    (dict(state=JobState.PARTIALLY_COMPLETED, lease=False, reason="interrupted_after_publication"), False, (RecoveryReason.TERMINAL_JOB, RecoveryReason.RECOVERABLE_INTERRUPTION), True),
    (dict(state=JobState.PARTIALLY_COMPLETED, lease=False, reason="validation_warnings"), False, (RecoveryReason.TERMINAL_JOB,), True),
    (dict(state=JobState.VALIDATED, lease=False, reason="validated"), False, (RecoveryReason.TERMINAL_JOB,), True),
])
def test_what_a_job_can_do_depends_on_its_state_and_whether_its_run_is_alive(made, active, reasons, resumable):
    assert classify_job(snapshot(**made), active) == (reasons, resumable)


class Evidence:
    def __init__(self, *views, jobs=(), projects=(PROJECT,), source=SourceState.SAME, policy=None, output=None, active=False, failures=0, identified=False):
        self.views, self.job_list, self.projects, self.source_state = views, jobs, projects, source
        self.policy, self.output_evidence, self.active, self.failures, self.identified = policy, output or OutputEvidence(), active, failures, identified
        self.source_calls = 0
        self.on_jobs = None

    def scan(self):
        return RecoveryScan(tuple(self.views), tuple(self.projects), self.failures)

    def jobs(self, project_id):
        if isinstance(self.job_list, Exception):
            raise self.job_list
        return tuple(self.job_list)

    def run_active(self, project_id, input_fingerprint):
        return self.active

    def source(self, reference, project_id, token):
        self.source_calls += 1
        if isinstance(self.source_state, Exception):
            raise self.source_state
        return self.source_state

    def policy_refusal(self, reference, project_id):
        return self.policy

    def output(self, reference, project_id, job_id, token):
        return self.output_evidence

    def stages(self, project_id, job_id):
        return ()

    def producer_identified(self, project_id, job_id):
        return self.identified


class Runner:
    def __init__(self, result=None):
        self.durable, self.legacy = [], []
        self.result = result

    def run_durable(self, reference, ticket, operation_id):
        self.durable.append((reference, ticket, operation_id))
        return self.result or TranslateProjectResult(PROJECT, job_id=NEXT, state=JobState.VALIDATED)

    def run_legacy(self, reference):
        self.legacy.append(reference)


LEGACY = InvocationView("legacy:a", True)
DURABLE = InvocationView("legacy:a", True, project_id=PROJECT, fingerprints=(FINGERPRINT,))
ACCEPTED = (RequiredDecision.FULL_REEXECUTION,)


def request(**changes):
    return ResumeRequest(**{"reference": "legacy:a", "accepted": ACCEPTED, **changes})


def test_a_file_with_no_project_is_legacy_and_gets_no_job_identity():
    listed = ListInterruptedJobs(Evidence(LEGACY)).execute().entries
    assert [entry.compatibility for entry in listed] == [CompatibilityStatus.LEGACY_ONLY]
    assert listed[0].reasons == (RecoveryReason.LEGACY_ONLY,) and listed[0].job_id is None and listed[0].project_id is None


def test_a_file_whose_project_has_no_job_on_its_plan_stays_legacy():
    entry = ListInterruptedJobs(Evidence(replace(DURABLE, fingerprints=(OTHER_FINGERPRINT,)), jobs=(snapshot(state=JobState.FAILED, lease=False, reason="interrupted"),))).execute().entries[0]
    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and entry.job_id is None and entry.project_id == PROJECT


def test_a_file_that_recomputes_to_a_job_is_durable_and_says_where_it_stands():
    entry = ListInterruptedJobs(Evidence(DURABLE, jobs=(snapshot(),))).execute().entries[0]
    assert entry.compatibility is CompatibilityStatus.DURABLE and entry.job_id == JOB and entry.expected_revision == 3
    assert entry.job_state is JobState.RUNNING and RecoveryReason.LEASE_OWNER_GONE in entry.reasons


def test_listing_hashes_no_book():
    evidence = Evidence(DURABLE, jobs=(snapshot(),))
    ListInterruptedJobs(evidence).execute()
    assert evidence.source_calls == 0


def test_a_file_that_finished_is_not_listed_even_if_its_job_is_interrupted():
    listed = ListInterruptedJobs(Evidence(replace(DURABLE, running=False), jobs=(snapshot(),))).execute().entries
    assert [entry.compatibility for entry in listed] == [CompatibilityStatus.JOB_ONLY]


def test_a_job_with_no_file_is_inspectable_but_has_nothing_to_run_with():
    entries = ListInterruptedJobs(Evidence(jobs=(snapshot(),))).execute().entries
    assert entries[0].compatibility is CompatibilityStatus.JOB_ONLY and not entries[0].resumable
    assert RecoveryReason.INVOCATION_UNAVAILABLE in entries[0].reasons


def test_only_the_newest_job_of_a_plan_counts():
    newer, older = snapshot(state=JobState.VALIDATED, lease=False, reason="validated", job_id=NEXT), snapshot(state=JobState.FAILED, lease=False, reason="interrupted")
    assert ListInterruptedJobs(Evidence(jobs=(newer, older))).execute().entries == ()


def test_a_continued_job_is_not_listed_again():
    parent = snapshot(state=JobState.FAILED, lease=False, reason="interrupted")
    child = snapshot(state=JobState.FAILED, lease=False, reason="interrupted", job_id=NEXT, continuation_of=JOB, fingerprint=OTHER_FINGERPRINT)
    entries = ListInterruptedJobs(Evidence(jobs=(child, parent))).execute().entries
    assert [entry.job_id for entry in entries] == [NEXT]


def test_a_finished_job_with_warnings_is_not_an_interruption_unless_a_file_says_so():
    done = snapshot(state=JobState.PARTIALLY_COMPLETED, lease=False, reason="validation_warnings")
    assert ListInterruptedJobs(Evidence(jobs=(done,))).execute().entries == ()
    assert ListInterruptedJobs(Evidence(DURABLE, jobs=(done,))).execute().entries[0].compatibility is CompatibilityStatus.DURABLE


@pytest.mark.parametrize("error, reason", [("version_gap", RecoveryReason.UNSUPPORTED_SCHEMA), ("io", RecoveryReason.PROJECT_UNAVAILABLE)])
def test_a_project_store_that_cannot_be_read_is_one_entry_and_not_the_end(error, reason):
    from ayvu.job import JobStoreError
    entries = ListInterruptedJobs(Evidence(LEGACY, jobs=JobStoreError(error))).execute().entries
    assert entries[0].compatibility is CompatibilityStatus.LEGACY_ONLY
    assert entries[1].reasons == (reason,) and not entries[1].resumable


def test_an_unusable_file_is_listed_as_unsupported_and_cannot_resume():
    view = InvocationView("legacy:a", False, problem=RecoveryReason.UNSUPPORTED_SCHEMA, unusable=True)
    listing = ListInterruptedJobs(Evidence(view, projects=())).execute()
    assert listing.entries[0].compatibility is CompatibilityStatus.UNSUPPORTED and listing.entries[0].reasons == (RecoveryReason.UNSUPPORTED_SCHEMA,)
    result = RequestJobResume(Evidence(view, projects=())).execute(request(), Runner())
    assert result.status is ResumeStatus.REFUSED


def test_listing_counts_projects_it_could_not_read():
    assert ListInterruptedJobs(Evidence(projects=(), failures=2)).execute().unreadable_projects == 2


def test_inspection_of_an_unknown_reference_is_none():
    assert InspectJobRecoverability(Evidence()).execute("legacy:nope") is None


def test_inspection_says_a_repeat_may_happen_and_asks_for_the_decision():
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),))).execute("legacy:a")
    assert seen.provider_calls_may_repeat and seen.required_decisions == ACCEPTED and seen.entry.resumable
    assert RecoveryReason.PROVENANCE_UNPROVEN in seen.entry.reasons
    assert seen.selection.resume_from is ResumeStage.TRANSLATE


def test_identified_lineage_is_not_reported_as_unproven():
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), identified=True)).execute("legacy:a")
    assert RecoveryReason.PROVENANCE_UNPROVEN not in seen.entry.reasons


def test_a_legacy_file_has_no_stage_selection_at_all():
    seen = InspectJobRecoverability(Evidence(LEGACY)).execute("legacy:a")
    assert seen.selection is None and seen.provider_calls_may_repeat and seen.required_decisions == ACCEPTED


@pytest.mark.parametrize("state, reason", [
    (SourceState.CHANGED, RecoveryReason.SOURCE_CHANGED), (SourceState.MISSING, RecoveryReason.SOURCE_MISSING), (SourceState.UNREADABLE, RecoveryReason.SOURCE_UNREADABLE),
])
def test_a_source_that_is_not_as_recorded_blocks_a_durable_resume(state, reason):
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), source=state)).execute("legacy:a")
    assert reason in seen.entry.reasons and not seen.entry.resumable and seen.required_decisions == ()


def test_on_the_legacy_path_only_a_proved_change_blocks():
    assert not InspectJobRecoverability(Evidence(LEGACY, source=SourceState.CHANGED)).execute("legacy:a").entry.resumable
    gone = InspectJobRecoverability(Evidence(LEGACY, source=SourceState.MISSING)).execute("legacy:a")
    assert gone.entry.resumable and RecoveryReason.SOURCE_MISSING in gone.entry.reasons


def test_policy_blocks_a_durable_resume_before_anything_runs():
    evidence = Evidence(DURABLE, jobs=(snapshot(),), policy="encrypted_content")
    runner = Runner()
    result = RequestJobResume(evidence).execute(request(), runner)
    assert result.status is ResumeStatus.REFUSED and result.code == "policy_denied" and not runner.durable


def test_policy_on_the_legacy_path_is_reported_and_the_current_flow_refuses_the_book():
    seen = InspectJobRecoverability(Evidence(LEGACY, policy="encrypted_content")).execute("legacy:a")
    assert RecoveryReason.POLICY_DENIED in seen.entry.reasons and seen.entry.resumable


@pytest.mark.parametrize("output, reason", [
    (OutputEvidence(ArtifactIntegrity.MISSING), RecoveryReason.ARTIFACT_MISSING),
    (OutputEvidence(ArtifactIntegrity.PARTIAL, exists=True), RecoveryReason.ARTIFACT_PARTIAL),
    (OutputEvidence(ArtifactIntegrity.CORRUPT, exists=True), RecoveryReason.ARTIFACT_CORRUPT),
    (OutputEvidence(conflict=True, exists=True), RecoveryReason.DESTINATION_CONFLICT),
])
def test_each_problem_with_the_output_has_its_own_reason_and_none_blocks(output, reason):
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), output=output)).execute("legacy:a")
    assert reason in seen.entry.reasons and seen.entry.resumable


def test_a_file_at_the_destination_needs_its_own_decision():
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), output=OutputEvidence(conflict=True, exists=True))).execute("legacy:a")
    assert RequiredDecision.REPLACE_DESTINATION in seen.required_decisions
    runner = Runner()
    result = RequestJobResume(Evidence(DURABLE, jobs=(snapshot(),), output=OutputEvidence(conflict=True, exists=True))).execute(request(), runner)
    assert result.code == "decision_required" and not runner.durable


def test_a_verified_output_is_not_reported_as_a_problem():
    seen = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), output=OutputEvidence(ArtifactIntegrity.VERIFIED, exists=True))).execute("legacy:a")
    assert not {RecoveryReason.ARTIFACT_MISSING, RecoveryReason.ARTIFACT_CORRUPT, RecoveryReason.DESTINATION_CONFLICT} & set(seen.entry.reasons)


def test_cancelling_a_long_read_says_so_and_decides_nothing():
    use_case = InspectJobRecoverability(Evidence(DURABLE, jobs=(snapshot(),), source=RecoveryCancelled()))
    seen = use_case.execute("legacy:a", cancellation=use_case.cancellation())
    assert seen.cancelled and seen.required_decisions == () and seen.selection is None


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
