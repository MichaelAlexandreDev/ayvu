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

from tests.support.translate_batch_project_contract_support import DIGEST, PROJECT, _options, _use_case


def labeled(tmp_path, monkeypatch):
    """A model label selects the rich runtime; its project paths are fixed here, since these ports are in memory."""
    import ayvu.composition.builders.translate_project as composition
    monkeypatch.setattr(composition, "ProjectJsonRepository", lambda _: SimpleNamespace(
        resolve_database_paths=lambda _: SimpleNamespace(database=tmp_path / "project.sqlite", backups=tmp_path / "backups"),
        resolve_artifact_root=lambda _: tmp_path / "artifacts"))
    return {"model_label": "local-model-2026", "cache_path": tmp_path / "cache.sqlite"}


def test_accepted_plan_passes_through_production_composition(tmp_path, monkeypatch):
    import ayvu.composition.builders.translate_project as composition

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
                                     accepted.output, False, lambda: None, accepted_output=accepted, **labeled(tmp_path, monkeypatch))
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
    from ayvu.translation.batch_output import OutputIntent
    import ayvu.composition.builders.translate_project as composition

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
                                     accepted.output, False, lambda: None, accepted_output=accepted, **labeled(tmp_path, monkeypatch))
    translate_in_project(translation, TranslateObserver())
    assert str(accepted.output.path) == "relative.epub"
    assert seen[0].output.path == tmp_path / "relative.epub"
    assert seen[0].output == replace(accepted.output, path=accepted.output.path.resolve())


def test_composition_rejects_a_different_destination_before_opening_project(tmp_path, monkeypatch):
    import ayvu.composition.builders.translate_project as composition

    accepted = BatchOutputPolicy().plan((intent(tmp_path, "book"),)).items[0]
    opened = []
    monkeypatch.setattr(composition, "open_translate_project", lambda *args, **kwargs: opened.append(args))
    translation = ProjectTranslation(tmp_path, PROJECT, DIGEST, tmp_path / "book.epub", _options(),
                                     OutputPlan(tmp_path / "unplanned.epub"), False, lambda: None,
                                     accepted_output=accepted, **labeled(tmp_path, monkeypatch))
    with pytest.raises(TranslateCommandError, match="output_plan_mismatch"):
        translate_in_project(translation, TranslateObserver())
    assert opened == []


def test_composition_binds_batch_request_to_the_active_project_source(tmp_path, monkeypatch):
    import ayvu.composition.builders.translate_project as composition

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
                                     OutputPlan(tmp_path / "output.epub"), False, lambda: None, **labeled(tmp_path, monkeypatch))
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

    monkeypatch.setattr("ayvu.translation.engine.TranslationEngine.run", engine_run)
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
    monkeypatch.setattr("ayvu.translation.engine.TranslationEngine.run", interrupt)
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
    monkeypatch.setattr("ayvu.translation.engine.TranslationEngine.run", interrupt)
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
    import ayvu.composition.builders.translate_project as composition

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
                                     False, lambda: None, accepted_output=accepted, **labeled(tmp_path, monkeypatch))
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
