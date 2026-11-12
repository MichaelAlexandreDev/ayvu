"""The recovery rules, over a fake of what they read. The stores are in tests/recovery/test_job_recovery_*_integration.py."""
from __future__ import annotations
import pytest
from ayvu.recovery.service import InspectJobRecoverability, OutputEvidence, RecoveryCancelled, RequestJobResume, RequiredDecision, ResumeStatus, SourceState
from ayvu.translation.resume_reuse import ArtifactIntegrity, RecoveryReason, ResumeStage
from tests.support.job_recovery_support import ACCEPTED, DURABLE, Evidence
from tests.support.job_recovery_support import LEGACY, Runner
from tests.support.job_recovery_support import request, snapshot


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


def test_a_changed_book_on_the_legacy_path_stays_blocked_when_policy_also_refuses_it():
    seen = InspectJobRecoverability(Evidence(LEGACY, source=SourceState.CHANGED, policy="encrypted_content")).execute("legacy:a")
    assert not seen.entry.resumable and {RecoveryReason.SOURCE_CHANGED, RecoveryReason.POLICY_DENIED} <= set(seen.entry.reasons)


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
