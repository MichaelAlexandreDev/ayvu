from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID
import pytest
from ayvu.jobs.model import JobState, acquire_lease, new_job, release_lease, transition_job
from ayvu.jobs.events import encode_event
from ayvu.translation.batch import ItemStatus
from ayvu.composition.builders.translate_batch import BatchCancellationController, project_item_outcome
from ayvu.translation.project import TranslateProject
from ayvu.translation.project_contracts import (TranslateCommandError, TranslateObserver, TranslateProjectCommand,
                                    TranslateProjectInterrupted, TranslateProjectPorts, TranslateProjectResult)
from ayvu.composition.builders.translate_project import ProjectTranslation, translate_in_project
from ayvu.translation.engine import ENGINE_BEFORE_UNIT, EngineResult
from tests.support.batch_output_policy_support import intent
from ayvu.translation.batch_output import BatchOutputPolicy
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions, JobFailure, JobFailureFamily

NOW = datetime(2026, 1, 1, tzinfo=UTC)


PROJECT = str(UUID(int=1))


JOB = str(UUID(int=2))


OWNER = "b" * 64


DIGEST = "a" * 64


def _options():
    return TranslationOptions(language_pair=LanguagePair("en", "pt"))


class MemoryJobs:
    def __init__(self):
        self.job = None

    def list_jobs(self, *args, **kwargs):
        return ()

    def create(self, request):
        self.job = new_job(request, JOB, NOW)
        return self.job

    def acquire_lease(self, job_id, duration):
        self.job = acquire_lease(self.job, OWNER, NOW, duration)
        return self.job

    def transition(self, job_id, state, *, expected_revision, summary=None):
        self.job = transition_job(self.job, state, expected_revision=expected_revision, now=NOW, summary=summary)
        return self.job

    def release_lease(self, job_id, token):
        self.job = release_lease(self.job, token, NOW)
        return self.job


class Publications:
    def reconcile(self):
        pass

    def publication(self, *args):
        return object()

    def find_ready(self, request):
        return None


class Runtime:
    def preflight(self):
        return None

    def resources(self, context):
        return nullcontext(SimpleNamespace(executor=object(), pool=None, documents=None))

    def facts(self):
        return None

    def classify(self, exc):
        return JobFailure(JobFailureFamily.INTERNAL, "unexpected_error")


class Guard:
    def exclusive(self, fingerprint):
        return nullcontext(True)


def _use_case():
    project = SimpleNamespace(source="en", target="pt", source_sha256=DIGEST,
                              profile_ref=None, provider_ref=None, policy_ref=None, glossary_ref=None,
                              configuration_ref=None, revision=1)
    jobs = MemoryJobs()
    ports = TranslateProjectPorts(projects=SimpleNamespace(load=lambda _: project), jobs=jobs,
        provenance=SimpleNamespace(append=lambda _: None), formats=SimpleNamespace(safe_points=lambda _: ()),
        publications=Publications(), validator=object(), source=SimpleNamespace(fingerprint=lambda: "sha256:" + DIGEST),
        runtime=Runtime(), runs=Guard())
    ids = count(10)
    return TranslateProject(ports, clock=lambda: NOW, new_id=lambda: str(UUID(int=next(ids))),
                            lease_renewal=timedelta(hours=1)), jobs
