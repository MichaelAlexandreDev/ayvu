"""The recovery rules, over a fake of what they read. The stores are in tests/recovery/test_job_recovery_*_integration.py."""
from __future__ import annotations
from dataclasses import replace
import pytest
from ayvu.jobs.model import JobState
from ayvu.recovery.service import CompatibilityStatus, InvocationView, ListInterruptedJobs, RequestJobResume, ResumeStatus
from ayvu.translation.resume_reuse import RecoveryReason
from tests.support.job_recovery_support import DURABLE, Evidence, JOB
from tests.support.job_recovery_support import LEGACY, NEXT, OTHER_FINGERPRINT, PROJECT, Runner
from tests.support.job_recovery_support import request, snapshot


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
    from ayvu.jobs.model import JobStoreError
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
