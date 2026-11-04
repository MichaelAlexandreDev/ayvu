"""One contract for every format adapter, including the production EPUB adapter of #144.

A subclass supplies a ``FormatHarness``. Which checks apply comes from the descriptor
alone, so an adapter cannot pass by claiming less than it does without being refused.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ayvu.artifact import ArtifactRequest
from ayvu.artifact_service import ARTIFACT_SAFE_POINTS
from ayvu.format_adapter import (
    MATERIALIZING_OPERATIONS, AliasVerdict, DetectionOutcome, ExtractionRequest, FormatOperation, FormatRequirements, OperationSupport,
    PreviewRequest, ReconstructionRequest, UnitTranslation,
)
from ayvu.format_local import LocalFormatSource, LocalOutputGuard
from ayvu.format_service import FORMAT_BEFORE_WORK, FormatAdapter, FormatPublication, FormatRegistration, FormatRegistry, FormatService, format_safe_points
from ayvu.job import JobRequest, JobState
from ayvu.job_events import JobFailureFamily
from ayvu.job_execution import CancellationSource
from ayvu.project_schema import open_artifact_service, open_job_repository

PROJECT = "00000000-0000-4000-8000-000000000127"
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@dataclass(frozen=True)
class FormatHarness:
    adapter: FormatAdapter
    registration: FormatRegistration
    sample: bytes
    changed: bytes
    extension: str


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


class FormatAdapterContract:
    @pytest.fixture
    def harness(self):
        raise NotImplementedError

    @pytest.fixture
    def env(self, tmp_path):
        return FormatEnv(tmp_path)

    @pytest.fixture
    def service(self, harness):
        registry = FormatRegistry()
        registry.register(harness.registration, harness.adapter)
        return FormatService(registry)

    @pytest.fixture
    def path(self, harness, tmp_path):
        target = tmp_path / f"input{harness.extension}"
        target.write_bytes(harness.sample)
        return target

    @staticmethod
    def _id(harness):
        return harness.adapter.descriptor.adapter_id

    @staticmethod
    def _experimental(harness, operation):
        return harness.adapter.descriptor.claim(operation).support is OperationSupport.EXPERIMENTAL

    def _extract(self, harness, service, source, **changes):
        descriptor = harness.adapter.descriptor
        request = ExtractionRequest(PROJECT, descriptor.content_scopes, **changes)
        return service.extract(self._id(harness), source, request)

    def _request(self, harness, operation, documents, translations):
        requirements = FormatRequirements(allow_experimental=self._experimental(harness, operation))
        kind = PreviewRequest if operation is FormatOperation.PREVIEW else ReconstructionRequest
        return kind(documents, translations, requirements)

    def _documents(self, harness, service, path):
        result = self._extract(harness, service, LocalFormatSource(path))
        assert result.failure is None
        return result.documents

    @staticmethod
    def _translate(documents):
        return tuple(UnitTranslation(unit.unit_id, f"translated {index}")
                     for index, unit in enumerate(unit for document in documents for unit in document.units))

    def _call(self, harness, service, env, operation, source, documents=None, translations=(), cancellation=None, publication=None):
        adapter_id = self._id(harness)
        experimental = self._experimental(harness, operation)
        if operation is FormatOperation.INSPECT:
            return service.inspect(adapter_id, source, allow_experimental=experimental, cancellation=cancellation)
        if operation is FormatOperation.VALIDATE:
            return service.validate(adapter_id, source, allow_experimental=experimental, cancellation=cancellation)
        if operation is FormatOperation.EXTRACT:
            request = ExtractionRequest(PROJECT, harness.adapter.descriptor.content_scopes, allow_experimental=experimental)
            return service.extract(adapter_id, source, request, cancellation=cancellation)
        publication = publication or env.publication()
        method = service.preview if operation is FormatOperation.PREVIEW else service.reconstruct
        return method(adapter_id, source, self._request(harness, operation, documents, translations), publication, cancellation=cancellation)

    def _inputs(self, harness, service, path, operation):
        if operation not in MATERIALIZING_OPERATIONS or not harness.adapter.descriptor.runs(FormatOperation.EXTRACT):
            return {}
        documents = self._documents(harness, service, path)
        return {"documents": documents, "translations": self._translate(documents)}

    def _running(self, harness):
        return [item for item in FormatOperation if harness.adapter.descriptor.runs(item)]

    def _require(self, harness, operation):
        if not harness.adapter.descriptor.runs(operation):
            pytest.skip(f"the descriptor does not claim {operation.value}")

    def test_the_descriptor_registers_and_resolves_only_itself(self, harness, service):
        assert [item.adapter_id for item in service._registry.descriptors()] == [self._id(harness)]
        assert service._registry.get(self._id(harness)) is harness.adapter

    def test_the_sample_is_detected(self, harness, service, path):
        result = service.detect(LocalFormatSource(path))
        assert (result.outcome, result.adapter_id) == (DetectionOutcome.MATCH, self._id(harness))

    def test_an_extension_alone_does_not_override_a_signature(self, harness, service, tmp_path):
        impostor = tmp_path / f"impostor{harness.extension}"
        impostor.write_bytes(b"\0" * 64)
        result = service.detect(LocalFormatSource(impostor))
        if harness.adapter.descriptor.signatures:
            assert (result.outcome, result.reason) == (DetectionOutcome.NO_MATCH, "signature_conflict")
        else:
            assert (result.outcome, result.reason) == (DetectionOutcome.MATCH, "extension_match")

    @pytest.mark.parametrize("operation", list(FormatOperation), ids=lambda item: item.value)
    def test_an_operation_the_descriptor_does_not_run_touches_nothing(self, harness, service, env, path, operation):
        if harness.adapter.descriptor.runs(operation):
            pytest.skip(f"the descriptor claims {operation.value}")
        source = CountingSource(LocalFormatSource(path))
        documents = self._documents(harness, service, path) if harness.adapter.descriptor.runs(FormatOperation.EXTRACT) else None
        if operation in MATERIALIZING_OPERATIONS and not documents:
            pytest.skip("there is no document to give it")
        publication = env.publication() if operation in MATERIALIZING_OPERATIONS else None
        result = self._call(harness, service, env, operation, source, documents, publication=publication)
        assert result.failure.family is JobFailureFamily.CAPABILITY
        assert result.failure.code in ("unsupported_operation", "unknown_operation")
        assert source.reads == 0 and env.publisher.calls == 0

    def test_experimental_operations_need_explicit_acceptance(self, harness, service, env, path):
        for operation in self._running(harness):
            if not self._experimental(harness, operation) or operation in MATERIALIZING_OPERATIONS:
                continue
            method = service.inspect if operation is FormatOperation.INSPECT else service.validate
            assert method(self._id(harness), LocalFormatSource(path)).failure.code == "experimental_not_allowed"

    def test_every_running_operation_succeeds_and_leaves_the_source_alone(self, harness, service, env, path):
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        for operation in self._running(harness):
            inputs = self._inputs(harness, service, path, operation)
            assert self._call(harness, service, env, operation, LocalFormatSource(path), **inputs).failure is None, operation
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_extraction_is_deterministic_and_reports_the_adapters_identity(self, harness, service, path):
        self._require(harness, FormatOperation.EXTRACT)
        first, second = self._documents(harness, service, path), self._documents(harness, service, path)
        assert first == second and first
        descriptor = harness.adapter.descriptor
        for document in first:
            assert document.adapter == descriptor.adapter and document.source_fingerprint == LocalFormatSource(path).fingerprint()
            assert all((unit.content.locator.schema, unit.content.locator.schema_version) in descriptor.locators
                       for unit in document.units)

    def test_the_inventory_lists_what_a_selection_can_name(self, harness, service, path):
        self._require(harness, FormatOperation.INSPECT)
        self._require(harness, FormatOperation.EXTRACT)
        experimental = self._experimental(harness, FormatOperation.INSPECT)
        inventory = service.inspect(self._id(harness), LocalFormatSource(path), allow_experimental=experimental).inventory
        keys = {item.logical_key for item in inventory.documents}
        assert keys, "an adapter that extracts must list its documents"
        assert {document.logical_key for document in self._documents(harness, service, path)} <= keys

    def test_extraction_follows_the_selection_it_is_given(self, harness, service, path):
        self._require(harness, FormatOperation.EXTRACT)
        chosen = self._documents(harness, service, path)[0].logical_key
        selected = self._extract(harness, service, LocalFormatSource(path), selection=(chosen,))
        assert [document.logical_key for document in selected.documents] == [chosen]

    @pytest.mark.parametrize("operation", list(FormatOperation), ids=lambda item: item.value)
    def test_a_source_that_changes_during_work_fails_without_output(self, harness, service, env, path, operation):
        self._require(harness, operation)
        inputs = self._inputs(harness, service, path, operation)
        publication = env.publication() if operation in MATERIALIZING_OPERATIONS else None
        source = ChangingSource(LocalFormatSource(path), path, harness.changed)
        result = self._call(harness, service, env, operation, source, publication=publication, **inputs)
        assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTEGRITY, "source_changed")
        if publication is not None:
            assert not env.is_ready(publication)

    @pytest.mark.parametrize("operation", list(FormatOperation), ids=lambda item: item.value)
    def test_a_source_rewritten_in_place_during_work_never_gives_a_result(self, harness, service, env, path, operation):
        self._require(harness, operation)
        inputs = self._inputs(harness, service, path, operation)
        publication = env.publication() if operation in MATERIALIZING_OPERATIONS else None
        source = ChangingSource(LocalFormatSource(path), path, harness.changed, in_place=True)
        result = self._call(harness, service, env, operation, source, publication=publication, **inputs)
        # An adapter that cannot parse what it now reads fails its own way, so only the outcome is fixed.
        assert result.failure is not None
        if publication is not None:
            assert not env.is_ready(publication)

    @pytest.mark.parametrize("operation", sorted(MATERIALIZING_OPERATIONS), ids=lambda item: item.value)
    def test_materializing_publishes_a_new_artifact_with_the_results(self, harness, service, env, path, operation):
        self._require(harness, operation)
        documents = self._documents(harness, service, path)
        translations = self._translate(documents)
        publication = env.publication()
        result = self._call(harness, service, env, operation, LocalFormatSource(path), documents, translations, publication=publication)
        assert result.artifact is not None and env.is_ready(publication)
        assert all(item.text.encode() in env.read(publication) for item in translations)
        assert path.read_bytes() == harness.sample

    def _refused(self, harness, service, env, path, request_changes=None, guard=None, source=None, documents=None, translations=None):
        documents = documents if documents is not None else self._documents(harness, service, path)
        translations = translations if translations is not None else self._translate(documents)
        publication = env.publication(guard)
        result = self._call(harness, service, env, FormatOperation.RECONSTRUCT, source or LocalFormatSource(path), documents, translations, publication=publication)
        assert result.artifact is None and not env.is_ready(publication) and env.publisher.calls == 0
        assert path.read_bytes() in (harness.sample, harness.changed)
        return result.failure

    def test_reconstruction_needs_a_result_for_every_unit(self, harness, service, env, path):
        self._require(harness, FormatOperation.RECONSTRUCT)
        documents = self._documents(harness, service, path)
        failure = self._refused(harness, service, env, path, documents=documents, translations=self._translate(documents)[1:])
        assert (failure.family, failure.code) == (JobFailureFamily.INTEGRITY, "incomplete_translations")

    def test_reconstruction_refuses_a_result_from_another_revision(self, harness, service, env, path):
        self._require(harness, FormatOperation.RECONSTRUCT)
        documents = self._documents(harness, service, path)
        stranger = UnitTranslation("not-a-unit-of-this-revision", "text")
        failure = self._refused(harness, service, env, path, documents=documents, translations=(*self._translate(documents), stranger))
        assert failure.code == "unknown_unit"

    def test_reconstruction_refuses_a_source_changed_since_extraction(self, harness, service, env, path):
        self._require(harness, FormatOperation.RECONSTRUCT)
        documents = self._documents(harness, service, path)
        path.write_bytes(harness.changed)
        failure = self._refused(harness, service, env, path, documents=documents)
        assert (failure.family, failure.code) == (JobFailureFamily.INTEGRITY, "source_changed")
        assert path.read_bytes() == harness.changed

    def test_reconstruction_refuses_a_document_with_no_fingerprint(self, harness, service, env, path):
        self._require(harness, FormatOperation.RECONSTRUCT)
        documents = self._documents(harness, service, path)
        stripped = tuple(replace(document, source_fingerprint=None) for document in documents)
        assert self._refused(harness, service, env, path, documents=stripped, translations=self._translate(documents)).code == "source_changed"

    @pytest.mark.parametrize("verdict,code", [(AliasVerdict.SAME, "output_is_source"), (AliasVerdict.UNKNOWN, "output_alias_unknown")])
    def test_reconstruction_refuses_a_destination_that_is_or_may_be_the_source(self, harness, service, env, path, verdict, code):
        self._require(harness, FormatOperation.RECONSTRUCT)
        failure = self._refused(harness, service, env, path, guard=FixedGuard(verdict))
        assert (failure.family, failure.code) == (JobFailureFamily.POLICY, code)

    @pytest.mark.parametrize("kind", ["same_path", "relative_path", "symlink", "hard_link"])
    def test_reconstruction_refuses_real_aliases_of_the_original(self, harness, service, env, path, kind):
        self._require(harness, FormatOperation.RECONSTRUCT)
        alias = path.parent / "alias"
        if kind == "same_path":
            alias = path
        elif kind == "relative_path":
            (path.parent / "sub").mkdir()
            alias = path.parent / "sub" / ".." / path.name
        elif kind == "symlink":
            alias.symlink_to(path)
        else:
            os.link(path, alias)
        assert self._refused(harness, service, env, path, guard=LocalOutputGuard(alias)).code == "output_is_source"

    @pytest.mark.parametrize("operation", list(FormatOperation), ids=lambda item: item.value)
    def test_cancelling_at_every_declared_safe_point_publishes_nothing(self, harness, service, env, path, operation):
        self._require(harness, operation)
        descriptor = harness.adapter.descriptor
        points = (FORMAT_BEFORE_WORK, *descriptor.claim(operation).safe_points)
        if operation in MATERIALIZING_OPERATIONS:
            points = (*points, *ARTIFACT_SAFE_POINTS)
        for point in points:
            inputs = self._inputs(harness, service, path, operation)
            publication = env.publication() if operation in MATERIALIZING_OPERATIONS else None
            token = TriggerToken(format_safe_points(descriptor), point)
            result = self._call(harness, service, env, operation, LocalFormatSource(path), cancellation=token, publication=publication, **inputs)
            assert token.reached, f"{point} is declared but never reached"
            assert result.failure.family is JobFailureFamily.CANCELLED, point
            if publication is not None:
                assert not env.is_ready(publication), point
        assert path.read_bytes() == harness.sample
