"""The recovery rules, over a fake of what they read. The stores are in tests/recovery/test_job_recovery_*_integration.py."""
from __future__ import annotations
import pytest
from ayvu.jobs.model import JobState
from ayvu.recovery.service import classify_job
from ayvu.translation.resume_reuse import REUSE_DIMENSIONS, ArtifactIntegrity, RecoveryReason, ResumeStage, select_resume_stage
from tests.support.job_recovery_support import snapshot, stage


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
