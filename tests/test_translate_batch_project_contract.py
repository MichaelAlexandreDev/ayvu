"""Project-boundary contracts with in-memory ports; no SQLite or network."""

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

from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions
from ayvu.job import JobState, acquire_lease, new_job, release_lease, transition_job
from ayvu.job_events import JobFailure, JobFailureFamily, encode_event
from ayvu.translate_batch import ItemStatus
from ayvu.translate_batch_composition import BatchCancellationController, project_item_outcome
from ayvu.translate_project import (TranslateCommandError, TranslateObserver, TranslateProject,
                                    TranslateProjectCommand, TranslateProjectInterrupted, TranslateProjectPorts,
                                    TranslateProjectResult)
from ayvu.translate_project_composition import ProjectTranslation, translate_in_project
from ayvu.translation_engine import ENGINE_BEFORE_UNIT, EngineResult
from test_batch_output_policy import intent
from ayvu.batch_output_policy import BatchOutputPolicy

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

    def resources(self):
        return nullcontext(SimpleNamespace(executor=object(), pool=None))

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


def test_accepted_plan_passes_through_production_composition(tmp_path, monkeypatch):
    import ayvu.translate_project_composition as composition

    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book"),)).items[0]
    seen = {}
    class FakeUseCase:
        def execute(self, command, **kwargs):
            seen["command"] = command
            return TranslateProjectResult(PROJECT, recorded=False)
    def open_case(*args, **kwargs):
        seen["guard"] = kwargs["before_publish"]
        return FakeUseCase()
    monkeypatch.setattr(composition, "open_translate_project", open_case)
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, tmp_path / "book.epub", _options(),
                                     accepted.output, False, lambda: None, lambda: None, accepted_output=accepted)
    translate_in_project(translation, TranslateObserver())
    assert seen["command"].accepted_output is accepted
    assert seen["command"].output == accepted.output
    assert seen["guard"].__self__ is accepted


def test_mismatched_plan_is_refused_before_any_port(tmp_path):
    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book"),)).items[0]
    with pytest.raises(TranslateCommandError, match="output_plan_mismatch"):
        TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / "other.epub"),
                                accepted_output=accepted)


def test_relative_accepted_output_keeps_canonical_resume_fingerprint(tmp_path, monkeypatch):
    from dataclasses import replace
    from ayvu.batch_output_policy import OutputIntent
    import ayvu.translate_project_composition as composition

    monkeypatch.chdir(tmp_path)
    requested = intent(tmp_path, "book")
    requested = OutputIntent(requested.source, OutputPlan(Path("relative.epub")),
                             requested.report_dir, requested.report_stem)
    accepted = BatchOutputPolicy().plan((requested,)).items[0]
    seen = []
    class UseCase:
        def execute(self, command, **kwargs):
            seen.append(command)
            return TranslateProjectResult(PROJECT, recorded=False)
    monkeypatch.setattr(composition, "open_translate_project", lambda *args, **kwargs: UseCase())
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, requested.source, _options(),
                                     accepted.output, False, lambda: None, lambda: None, accepted_output=accepted)
    translate_in_project(translation, TranslateObserver())
    assert str(accepted.output.path) == "relative.epub"
    assert seen[0].output.path == tmp_path / "relative.epub"
    assert seen[0].output == replace(accepted.output, path=accepted.output.path.resolve())


def test_composition_rejects_a_different_destination_before_opening_project(tmp_path, monkeypatch):
    import ayvu.translate_project_composition as composition

    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book"),)).items[0]
    opened = []
    monkeypatch.setattr(composition, "open_translate_project", lambda *args, **kwargs: opened.append(args))
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, tmp_path / "book.epub", _options(),
                                     OutputPlan(tmp_path / "unplanned.epub"), False, lambda: None, lambda: None,
                                     accepted_output=accepted)
    with pytest.raises(TranslateCommandError, match="output_plan_mismatch"):
        translate_in_project(translation, TranslateObserver())
    assert opened == []


def test_composition_binds_batch_request_to_the_active_project_source(tmp_path, monkeypatch):
    import ayvu.translate_project_composition as composition

    use_case, jobs = _use_case()
    controller = BatchCancellationController()
    original_execute = use_case.execute
    sources = []

    def execute(command, *, cancellation, **kwargs):
        sources.append(cancellation)
        controller.request()
        assert cancellation.requested is not None
        return original_execute(command, cancellation=cancellation, **kwargs)

    use_case.execute = execute
    monkeypatch.setattr(composition, "open_translate_project", lambda *args, **kwargs: use_case)
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, tmp_path / "book.epub", _options(),
                                     OutputPlan(tmp_path / "output.epub"), False, lambda: None, lambda: None)
    run = translate_in_project(translation, TranslateObserver(), cancellation=controller)
    result = run.result
    assert len(sources) == 1
    assert result.state is jobs.job.state is JobState.CANCELLED
    assert run.report is None
    assert controller.checkpoint("batch_before_item") is not None


def test_real_project_active_cancellation_is_observed_after_blocking_transport(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    use_case, jobs = _use_case()
    controller = BatchCancellationController()
    source = use_case.cancellation()

    def engine_run(engine, request):
        entered.set()
        assert release.wait(5)
        assert engine._cancellation.checkpoint(ENGINE_BEFORE_UNIT) is not None
        return EngineResult(failure=JobFailure(JobFailureFamily.CANCELLED, "cancelled"))

    monkeypatch.setattr("ayvu.translate_project.TranslationEngine.run", engine_run)
    command = TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / "out.epub"))
    def run():
        with controller.bind(source):
            return use_case.execute(command, cancellation=source)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        try:
            assert entered.wait(5)
            controller.request()
            assert source.requested is not None and not future.done()
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.state is JobState.CANCELLED and jobs.job.state is JobState.CANCELLED
    assert project_item_outcome(result).status is ItemStatus.CANCELLED


def test_interrupted_exception_carries_settled_real_result(tmp_path, monkeypatch):
    use_case, jobs = _use_case()
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr("ayvu.translate_project.TranslationEngine.run", interrupt)
    command = TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / "out.epub"))
    with pytest.raises(TranslateProjectInterrupted) as raised:
        use_case.execute(command)
    result = raised.value.result
    assert result.state is jobs.job.state is JobState.CANCELLED
    assert result.outcome.state is JobState.CANCELLED and result.recorded
    assert not result.output_ready
    assert project_item_outcome(result, interrupted=True).status is ItemStatus.CANCELLED


@pytest.mark.parametrize("family,code,fast,expected", [
    (JobFailureFamily.INPUT, "invalid_book", False, "input_failed"),
    (JobFailureFamily.PROVIDER_TRANSIENT, "timeout", False, "provider_failed"),
    (JobFailureFamily.PROVIDER_PERMANENT, "rejected", True, "fail_fast"),
    (JobFailureFamily.INTEGRITY, "destination_changed", False, "output_conflict"),
    (JobFailureFamily.RESOURCE, "validation_unavailable", False, "validation_failed"),
    (JobFailureFamily.INTERNAL, "unexpected_error", False, "translation_failed"),
])
def test_typed_project_failures_are_normalized(family, code, fast, expected):
    result = TranslateProjectResult(PROJECT, state=JobState.FAILED, failure=JobFailure(family, code))
    outcome = project_item_outcome(result, fail_fast=fast)
    assert outcome.status is ItemStatus.FAILED and outcome.failure == expected
    assert outcome.artifact_ref is None and outcome.report_ref is None


def test_batch_public_projection_and_events_exclude_private_canaries(tmp_path, monkeypatch, caplog):
    canary = "private_canary_text_7fa31"
    use_case, _ = _use_case()
    def interrupt(*args):
        raise KeyboardInterrupt(canary)
    monkeypatch.setattr("ayvu.translate_project.TranslationEngine.run", interrupt)
    command = TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / f"{canary}.epub"))
    events = []
    with caplog.at_level("DEBUG"), pytest.raises(TranslateProjectInterrupted) as raised:
        use_case.execute(command, observer=TranslateObserver(on_event=events.append))
    outcome = project_item_outcome(raised.value.result, interrupted=True)
    serialized = repr(asdict(outcome)) + repr(outcome) + "".join(encode_event(event) for event in events) + caplog.text
    assert events and canary not in serialized


def test_stale_accepted_plan_is_refused_before_project_or_job_port(tmp_path):
    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book"),)).items[0]
    accepted.output.path.write_bytes(b"appeared after planning")
    use_case, jobs = _use_case()
    def forbidden(*args):
        pytest.fail("project port accessed before plan revalidation")
    use_case._ports.projects.load = forbidden
    command = TranslateProjectCommand(PROJECT, DIGEST, _options(), accepted.output, accepted_output=accepted)
    result = use_case.execute(command)
    assert result.failure.code == "destination_changed"
    assert jobs.job is None
    assert project_item_outcome(result).failure == "output_conflict"


def test_accepted_overwrite_reaches_publication_and_command(tmp_path, monkeypatch):
    import ayvu.translate_project_composition as composition

    planned = intent(tmp_path, "book", overwrite=True)
    planned.output.path.write_bytes(b"previous output")
    accepted = BatchOutputPolicy().plan((planned,)).items[0]
    seen = {}
    class FakeUseCase:
        def execute(self, command, **kwargs):
            seen["command"] = command
            return TranslateProjectResult(PROJECT, recorded=False)
    def open_case(*args, **kwargs):
        seen["overwrite"] = kwargs["overwrite"]
        return FakeUseCase()
    monkeypatch.setattr(composition, "open_translate_project", open_case)
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, planned.source, _options(), accepted.output,
                                     False, lambda: None, lambda: None, accepted_output=accepted)
    translate_in_project(translation, TranslateObserver())
    assert accepted.overwrite is True
    assert seen["overwrite"] is True and seen["command"].overwrite is True
    assert seen["command"].accepted_output is accepted
    assert planned.output.path.read_bytes() == b"previous output"


@pytest.mark.parametrize("accepted_overwrite", [False, True])
def test_command_rejects_overwrite_different_from_accepted_plan(tmp_path, accepted_overwrite):
    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book", overwrite=accepted_overwrite),)).items[0]
    with pytest.raises(TranslateCommandError, match="output_plan_mismatch"):
        TranslateProjectCommand(PROJECT, DIGEST, _options(), accepted.output,
                                overwrite=not accepted_overwrite, accepted_output=accepted)


@pytest.mark.parametrize("callback", ["on_chapter_start", "on_chapter_done", "on_text_processed"])
def test_dry_run_observes_cancellation_in_each_callback_before_publication(tmp_path, monkeypatch, callback):
    import ayvu.translate_project_composition as composition

    cancellation = BatchCancellationController()
    seen = []
    output = tmp_path / "output.epub"
    def fake_translate(source, destination, **kwargs):
        cancellation.request()
        kwargs[callback]("callback argument")
        output.write_bytes(b"should not be published")
        return object()
    monkeypatch.setattr(composition, "translate_epub", fake_translate)
    with pytest.raises(composition.TranslationDryRunCancelled):
        composition.translate_without_project(tmp_path / "book.epub", output, cancellation=cancellation,
                                              **{callback: lambda *args: seen.append(args)})
    assert seen == []
    assert not output.exists()
