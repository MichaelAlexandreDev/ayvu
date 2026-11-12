"""One contract for every format adapter, including the production EPUB adapter of #144.

A subclass supplies a ``FormatHarness``. Which checks apply comes from the descriptor
alone, so an adapter cannot pass by claiming less than it does without being refused.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ayvu.artifacts.model import ArtifactRequest
from ayvu.artifacts.service import ARTIFACT_SAFE_POINTS
from ayvu.formats.contracts import (
    MATERIALIZING_OPERATIONS, AliasVerdict, DetectionOutcome, ExtractionRequest, FormatOperation, FormatRequirements, OperationSupport,
    PreviewRequest, ReconstructionRequest, UnitTranslation,
)
from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
from ayvu.formats.service import FORMAT_BEFORE_WORK, FormatAdapter, FormatPublication, FormatRegistration, FormatRegistry, FormatService, format_safe_points
from ayvu.jobs.model import JobRequest, JobState
from ayvu.jobs.execution import CancellationSource
from ayvu.projects.schema import open_artifact_service, open_job_repository
from ayvu.kernel import JobFailureFamily

PROJECT = "00000000-0000-4000-8000-000000000127"
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _plain_translation(unit, index):
    return f"translated {index}"


def _as_published(data):
    return data


@dataclass(frozen=True)
class FormatHarness:
    adapter: FormatAdapter
    registration: FormatRegistration
    sample: bytes
    changed: bytes
    extension: str
    # A format may need tokens kept in the text, and may compress what it writes.
    translation_of: Callable = _plain_translation
    readable_output: Callable = _as_published


class FixedGuard:
    def __init__(self, verdict):
        self._verdict = verdict

    def verdict(self, source):
        return self._verdict


class SpyPublisher:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def publish(self, request, chunks, **options):
        self.calls += 1
        return self.inner.publish(request, chunks, **options)


class CountingSource:
    """Counts every read, so "before any work" is something a test can see."""

    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    @property
    def extension(self):
        return self.inner.extension

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name in ("size", "fingerprint", "head", "open"):
            def counted(*args):
                self.reads += 1
                return attribute(*args)
            return counted
        return attribute


class ChangingSource(CountingSource):
    """The file is changed as soon as an adapter opens it, after the check it passed.

    Replacing it leaves the adapter reading the old content. Rewriting it in place means
    the adapter may read anything, including something it cannot parse.
    """

    def __init__(self, inner, path, changed, *, in_place=False):
        super().__init__(inner)
        self.path, self.changed, self.in_place, self.armed = path, changed, in_place, True

    def open(self):
        stream = self.inner.open()
        if self.armed:
            self.armed = False
            if self.in_place:
                self.path.write_bytes(self.changed)
            else:
                replacement = self.path.with_name("replacement")
                replacement.write_bytes(self.changed)
                os.replace(replacement, self.path)
        return stream


class TriggerToken:
    """Asks for cancellation exactly when one safe point is reached."""

    def __init__(self, safe_points, target):
        self.source = CancellationSource(safe_points)
        self.target = target
        self.reached = False

    def checkpoint(self, safe_point):
        if safe_point == self.target:
            self.reached = True
            self.source.request("user_requested", NOW)
        return self.source.token.checkpoint(safe_point)


class FormatEnv:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        database, backups = tmp_path / "project.sqlite", tmp_path / "backups"
        self.count = 7000
        jobs = open_job_repository(database, backups, PROJECT, clock=lambda: NOW)
        self.artifacts = open_artifact_service(database, backups, tmp_path / "artifacts", PROJECT, clock=lambda: NOW, new_id=self.new_id)
        job = jobs.create(JobRequest(PROJECT, "translate_epub", 1, "sha256:" + "e" * 64))
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = jobs.transition(job.job_id, state, expected_revision=job.revision)
        self.jobs = jobs
        self.job = jobs.acquire_lease(job.job_id, timedelta(minutes=30))
        self.publisher = SpyPublisher(self.artifacts)
        self.requests = 0

    def new_id(self):
        self.count += 1
        return str(UUID(int=self.count))

    def publication(self, guard=None):
        self.requests += 1
        request = ArtifactRequest(PROJECT, self.job.job_id, "format_output", "materialize", 1, f"sha256:{self.requests:064x}")
        return FormatPublication(self.publisher, request, self.job.lease.owner_token, guard or FixedGuard(AliasVerdict.DISTINCT))

    def is_ready(self, publication):
        return self.artifacts.find_reusable(publication.request).artifact is not None

    def read(self, publication):
        artifact = self.artifacts.find_reusable(publication.request).artifact
        with self.artifacts.open_reusable(artifact.artifact_id, publication.request) as stream:
            return stream.read()


