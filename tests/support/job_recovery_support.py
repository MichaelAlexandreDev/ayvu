from __future__ import annotations
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import pytest
from ayvu.jobs.model import JobLease, JobRequest, JobState, JobTerminalSummary, TranslationJob
from ayvu.recovery.service import CompatibilityStatus, InspectJobRecoverability, InvocationView, ListInterruptedJobs, OutputEvidence, RecoveryCancelled, RecoveryScan, RequestJobResume, RequiredDecision, ResumeRequest, ResumeStatus, SourceState, classify_job
from ayvu.translation.resume_reuse import REUSE_DIMENSIONS, ArtifactIntegrity, RecoveryReason, ResumeStage, StageEvidence, select_resume_stage
from ayvu.jobs.service import JobSnapshot
from ayvu.translation.project_contracts import TranslateProjectResult
from ayvu.kernel import JobFailure, JobFailureFamily

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
