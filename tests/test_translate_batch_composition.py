"""Tests of the local batch binding without a CLI or a network service."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import UUID

import pytest

from ayvu.artifact import Artifact, ArtifactRequest, ArtifactState
from ayvu.batch_output_policy import BatchOutputError
from ayvu.job import JobState
from ayvu.job_execution import CancellationSource
from ayvu.translate_batch import BatchCommand, BatchValidationError, ItemOutcome, ItemStatus
from ayvu.translate_batch_composition import (
    BatchCancellationController, LocalBatchItem, LocalTranslateBatch, project_item_outcome,
)
from ayvu.translate_project import TranslateProjectResult
from test_batch_output_policy import intent


def _id(number):
    return str(UUID(int=number))


def _published_result():
    now = datetime(2024, 1, 1, tzinfo=UTC)
    request = ArtifactRequest(_id(1), _id(2), "translated_epub", "translation", 1, "sha256:" + "a" * 64)
    artifact = Artifact(_id(3), request, ArtifactState.READY, 1, now, now, size=10, digest="sha256:" + "b" * 64)
    return TranslateProjectResult(project_id=_id(1), job_id=_id(2), state=JobState.VALIDATED,
                                  artifact=artifact, report_ref=_id(4))


def test_project_result_reports_durable_publication_and_saved_report():
    project = _published_result()
    outcome = project_item_outcome(project, report_saved=True)
    assert outcome.status is ItemStatus.SUCCESS
    assert (outcome.job_ref, outcome.artifact_ref, outcome.report_ref) == (_id(2), _id(3), _id(4))
    assert outcome.job_state == "validated" and outcome.output_published
    assert project_item_outcome(project).report_ref is None


def test_report_failure_preserves_published_job_facts_without_publication_references():
    outcome = project_item_outcome(_published_result(), report_failed=True)
    assert outcome.status is ItemStatus.FAILED
    assert outcome.failure == "report_failed"
    assert outcome.job_ref == _id(2)
    assert outcome.job_state == "validated" and outcome.output_published
    assert outcome.artifact_ref is None and outcome.report_ref is None


def test_local_binding_resolves_item_reference_independently_of_ordinal(tmp_path):
    called = []
    first, second = intent(tmp_path, "first"), intent(tmp_path, "second")

    def callback(name):
        def run(plan, correlation, ordinal, cancellation):
            called.append((name, plan.output.path, correlation, ordinal, cancellation))
            return ItemOutcome(ItemStatus.SUCCESS)
        return run

    service = LocalTranslateBatch((LocalBatchItem(first, callback("first")),
                                   LocalBatchItem(second, callback("second"))))
    plans = service.plan(service.command)
    # An ordinal is presentation metadata: it must not select the callback.
    service.execute(service.command.items[1], plans[1], _id(7), 1, cancellation=service.cancellation)
    assert called == [("second", second.output.path, _id(7), 1, service.cancellation)]
    called.clear()
    result = service.use_case.execute(service.command)
    assert result.succeeded
    assert [(name, path, ordinal) for name, path, _, ordinal, _ in called] == [
        ("first", first.output.path, 1), ("second", second.output.path, 2),
    ]
    assert len({call[2] for call in called}) == 2


def test_complete_planning_refuses_collision_before_callbacks(tmp_path):
    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    from ayvu.batch_output_policy import OutputIntent
    duplicate = OutputIntent(two.source, one.output, two.report_dir, two.report_stem, overwrite=True)
    calls = []
    service = LocalTranslateBatch((LocalBatchItem(one, lambda *args: calls.append(args)),
                                   LocalBatchItem(duplicate, lambda *args: calls.append(args))))
    with pytest.raises(BatchOutputError):
        service.use_case.execute(service.command)
    assert not calls
    with pytest.raises(BatchValidationError, match="invalid_command"):
        service.plan(BatchCommand((service.command.items[1],)))


def test_request_is_delegated_while_transport_is_blocked_then_stops_next_item(tmp_path):
    entered, release = Event(), Event()
    calls = []
    controller = BatchCancellationController()
    source = CancellationSource(("after_transport",))

    def blocking_transport():
        entered.set()
        assert release.wait(5)

    def first(plan, correlation, ordinal, cancellation):
        calls.append(ordinal)
        with cancellation.bind(source):
            blocking_transport()
            acknowledgement = source.token.checkpoint("after_transport")
            assert acknowledgement is not None and acknowledgement.reason_code == "batch_cancelled"
            return ItemOutcome(ItemStatus.CANCELLED)

    def second(*args):
        calls.append(2)
        return ItemOutcome(ItemStatus.SUCCESS)

    service = LocalTranslateBatch((LocalBatchItem(intent(tmp_path, "one"), first),
                                   LocalBatchItem(intent(tmp_path, "two"), second)), cancellation=controller)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.use_case.execute, service.command)
        try:
            assert entered.wait(5)
            controller.request()
            assert source.requested is not None
            assert not future.done()  # In-flight work is cooperative, not forcibly interrupted.
        finally:
            release.set()
        result = future.result(timeout=5)
    assert calls == [1]
    assert result.cancelled and len(result.items) == 1
    assert controller.checkpoint("batch_before_item") is not None


def test_request_after_item_starts_prevents_next_even_if_active_item_finishes(tmp_path):
    controller = BatchCancellationController()
    source = CancellationSource(("safe_point",))
    calls = []

    def first(plan, correlation, ordinal, cancellation):
        with cancellation.bind(source):
            calls.append(ordinal)
            cancellation.request()
            # A completed operation can have no further safe point at which to observe the request.
            return ItemOutcome(ItemStatus.SUCCESS)

    def second(*args):
        calls.append(2)
        return ItemOutcome(ItemStatus.SUCCESS)

    service = LocalTranslateBatch((LocalBatchItem(intent(tmp_path, "one"), first),
                                   LocalBatchItem(intent(tmp_path, "two"), second)), cancellation=controller)
    result = service.use_case.execute(service.command)
    assert source.requested is not None and source.acknowledgement is None
    assert calls == [1]
    assert result.cancelled and len(result.items) == 1
    assert result.items[0].outcome.status is ItemStatus.SUCCESS


@pytest.mark.parametrize("canary", [
    "Trecho confidencial do documento: a raposa escapou",
    "senha-super-secreta-94f2",
    "https://private-provider.example.test/translate?token=secret",
    '{"provider_response":"raw secret translation output"}',
])
def test_project_private_errors_never_reach_batch_results_events_or_logs(tmp_path, caplog, canary):
    import json
    import logging
    from dataclasses import asdict, replace

    from ayvu.translation_engine import EngineResult, TextStats

    project = replace(_published_result(), raised=RuntimeError(canary),
                      engine=EngineResult(stats=TextStats(errors=[canary])))
    normalized = project_item_outcome(project, report_saved=True)
    assert normalized.status is ItemStatus.PARTIAL
    assert normalized.warnings == ("texts_failed",)
    assert normalized.output_published
    assert normalized.artifact_ref == _id(3) and normalized.report_ref == _id(4)

    events = []
    service = LocalTranslateBatch((LocalBatchItem(intent(tmp_path, "book"),
                                                   lambda *args: project_item_outcome(project, report_saved=True)),),
                                  on_progress=events.append)
    result = service.use_case.execute(service.command)
    assert len(events) == 1 and result.items[0] == events[0]
    assert result.items[0].outcome == normalized
    surfaces = []
    logger = logging.getLogger("ayvu.test.batch.privacy")
    with caplog.at_level(logging.INFO, logger=logger.name):
        for value in (normalized, result, events[0]):
            rendered = repr(value)
            serialized = json.dumps(asdict(value), ensure_ascii=False)
            surfaces.extend((rendered, serialized))
            logger.info("result=%r json=%s", value, serialized)
    assert len(caplog.records) == 3
    assert all(canary not in surface for surface in (*surfaces, caplog.text))
