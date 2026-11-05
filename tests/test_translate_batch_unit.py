"""Contract tests for bounded, sequential batch orchestration, without adapters."""

from itertools import count
from uuid import UUID

import pytest

from ayvu.translate_batch import (
    BatchCommand,
    BatchItem,
    BatchValidationError,
    ItemOutcome,
    ItemStatus,
    TranslateBatch,
)


class Planner:
    def __init__(self):
        self.commands = []
        self.plans = ()

    def plan(self, command):
        self.commands.append(command)
        self.plans = tuple(object() for _ in command.items)
        return self.plans


class Executor:
    def __init__(self, statuses=()):
        self.statuses = iter(statuses)
        self.calls = []
        self.active = 0
        self.max_active = 0

    def execute(self, item, plan, correlation_id, ordinal, *, cancellation=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((item, plan, correlation_id, ordinal))
            status = next(self.statuses, ItemStatus.SUCCESS)
            return ItemOutcome(status, warnings=("warning",) if status is ItemStatus.PARTIAL else (),
                               failure="failure" if status is ItemStatus.FAILED else None)
        finally:
            self.active -= 1


class Cancellation:
    def __init__(self, cancel_at):
        self.cancel_at = cancel_at
        self.calls = []

    def checkpoint(self, name):
        self.calls.append(name)
        return object() if len(self.calls) >= self.cancel_at else None


def command(size, **kwargs):
    return BatchCommand(tuple(BatchItem(str(UUID(int=2*n+1)), str(UUID(int=2*n+2))) for n in range(size)), **kwargs)


def use_case(planner, executor, **kwargs):
    ids = count(1)
    return TranslateBatch(planner, executor, lambda: str(UUID(int=next(ids))), **kwargs)


@pytest.mark.parametrize("size", [0, 101])
def test_count_rejected_before_planning_or_execution(size):
    planner, executor = Planner(), Executor()
    with pytest.raises(BatchValidationError):
        use_case(planner, executor).execute(command(size))
    assert planner.commands == []
    assert executor.calls == []


@pytest.mark.parametrize("size", [1, 2, 100])
def test_order_accepted_plans_and_distinct_correlations(size):
    planner, executor = Planner(), Executor()
    requested = command(size)
    progress = []
    result = use_case(planner, executor, on_progress=progress.append).execute(requested)
    assert planner.commands == [requested]
    assert [call[0] for call in executor.calls] == list(requested.items)
    assert [call[1] for call in executor.calls] == list(planner.plans)
    assert [call[3] for call in executor.calls] == list(range(1, size + 1))
    assert len({call[2] for call in executor.calls}) == size
    assert [item.ordinal for item in result.items] == list(range(1, size + 1))
    assert list(result.items) == progress
    assert result.succeeded
    assert executor.max_active == 1


def test_missing_output_rejected_for_multiple_before_any_port():
    planner, executor = Planner(), Executor()
    with pytest.raises(BatchValidationError):
        use_case(planner, executor).execute(command(2, missing_output=True))
    assert planner.commands == []
    assert executor.calls == []


@pytest.mark.parametrize("continuation,expected", [(False, 2), (True, 3)])
def test_terminal_failure_stops_or_continues_and_aggregate_fails(continuation, expected):
    planner = Planner()
    executor = Executor([ItemStatus.SUCCESS, ItemStatus.FAILED, ItemStatus.SUCCESS])
    result = use_case(planner, executor).execute(command(3, continue_on_error=continuation))
    assert len(executor.calls) == expected
    assert len(result.items) == expected
    assert result.items[1].outcome.failure == "failure"
    assert not result.succeeded


def test_partial_remains_partial_with_warning_and_does_not_stop():
    planner, executor = Planner(), Executor([ItemStatus.PARTIAL, ItemStatus.SUCCESS])
    result = use_case(planner, executor).execute(command(2))
    assert len(executor.calls) == 2
    assert result.items[0].outcome.status is ItemStatus.PARTIAL
    assert result.items[0].outcome.warnings == ("warning",)
    assert result.succeeded


@pytest.mark.parametrize("cancel_at,started", [(1, 0), (2, 1)])
def test_cancellation_between_items_starts_no_later_item(cancel_at, started):
    planner, executor = Planner(), Executor()
    cancellation = Cancellation(cancel_at)
    result = use_case(planner, executor, cancellation=cancellation).execute(command(3))
    assert len(executor.calls) == started
    assert len(result.items) == started
    assert result.cancelled
    assert not result.succeeded
    assert cancellation.calls == ["batch_before_item"] * cancel_at


def test_cancelled_item_stops_even_when_continue_requested():
    planner, executor = Planner(), Executor([ItemStatus.CANCELLED, ItemStatus.SUCCESS])
    result = use_case(planner, executor).execute(command(2, continue_on_error=True))
    assert len(executor.calls) == 1
    assert result.items[0].outcome.status is ItemStatus.CANCELLED
    assert not result.succeeded


def test_references_reject_sensitive_values_and_repr_omits_them():
    item = BatchItem(str(UUID(int=1)), str(UUID(int=2)))
    assert item.source_ref not in repr(item)
    assert item.output_ref not in repr(BatchCommand((item,)))
    with pytest.raises(BatchValidationError, match="invalid_reference") as caught:
        BatchItem("secret-source-canary", str(UUID(int=2)))
    assert "secret-source-canary" not in str(caught.value)


def test_planner_refusal_never_invokes_executor():
    class RefusingPlanner:
        def plan(self, command):
            raise BatchValidationError("destination_conflict")

    executor = Executor()
    with pytest.raises(BatchValidationError, match="destination_conflict"):
        use_case(RefusingPlanner(), executor).execute(command(2))
    assert executor.calls == []


def test_unsupported_version_rejected_without_touching_ports():
    planner, executor = Planner(), Executor()
    with pytest.raises(BatchValidationError):
        use_case(planner, executor).execute(command(1, version=2))
    assert planner.commands == []
    assert executor.calls == []


def test_unrecognized_failure_and_warning_cannot_leak_in_results():
    with pytest.raises(BatchValidationError) as failure:
        ItemOutcome(ItemStatus.FAILED, failure="secretcanary")
    assert "secretcanary" not in str(failure.value)
    with pytest.raises(BatchValidationError) as warning:
        ItemOutcome(ItemStatus.PARTIAL, warnings=("secretcanary",))
    assert "secretcanary" not in str(warning.value)


def test_active_executor_receives_cancellation_and_stops_the_batch():
    cancellation = Cancellation(99)

    class ActiveExecutor(Executor):
        def execute(self, item, plan, correlation_id, ordinal, *, cancellation=None):
            self.calls.append((item, plan, correlation_id, ordinal, cancellation))
            assert cancellation.checkpoint("active_safe_point") is None
            return ItemOutcome(ItemStatus.CANCELLED)

    executor = ActiveExecutor()
    progress = []
    result = use_case(Planner(), executor, cancellation=cancellation, on_progress=progress.append).execute(
        command(2, continue_on_error=True))
    assert len(executor.calls) == 1
    assert executor.calls[0][4] is cancellation
    assert cancellation.calls == ["batch_before_item", "active_safe_point"]
    assert result.cancelled and not result.succeeded
    assert len(progress) == 1 and progress[0].outcome.status is ItemStatus.CANCELLED


def test_executor_receives_none_when_no_cancellation_is_supplied():
    class CaptureExecutor(Executor):
        def execute(self, item, plan, correlation_id, ordinal, *, cancellation):
            assert cancellation is None
            return ItemOutcome(ItemStatus.SUCCESS)

    assert use_case(Planner(), CaptureExecutor()).execute(command(1)).succeeded


def test_structured_references_survive_result_and_progress_without_repr_exposure():
    from dataclasses import asdict

    refs = tuple(str(UUID(int=n)) for n in (101, 102, 103))
    outcome = ItemOutcome(ItemStatus.SUCCESS, job_ref=refs[0], artifact_ref=refs[1], report_ref=refs[2])

    class ReferenceExecutor(Executor):
        def execute(self, item, plan, correlation_id, ordinal, *, cancellation=None):
            return outcome

    events = []
    result = use_case(Planner(), ReferenceExecutor(), on_progress=events.append).execute(command(1))
    stored = result.items[0].outcome
    assert (stored.job_ref, stored.artifact_ref, stored.report_ref) == refs
    assert events == list(result.items)
    assert all(ref not in repr(result) and ref not in repr(events) for ref in refs)
    serialized = asdict(result)
    assert serialized["items"][0]["outcome"]["job_ref"] == refs[0]
    assert serialized["items"][0]["outcome"]["artifact_ref"] == refs[1]
    assert serialized["items"][0]["outcome"]["report_ref"] == refs[2]


@pytest.mark.parametrize("field", ["job_ref", "artifact_ref", "report_ref"])
def test_reference_rejects_sensitive_values_without_disclosing_them(field):
    secret = "/private/secret-book.epub"
    with pytest.raises(BatchValidationError) as caught:
        ItemOutcome(ItemStatus.SUCCESS, **{field: secret})
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.parametrize("status", [ItemStatus.FAILED, ItemStatus.CANCELLED])
@pytest.mark.parametrize("field", ["artifact_ref", "report_ref"])
def test_unsuccessful_item_cannot_expose_publication_references(status, field):
    kwargs = {field: str(UUID(int=42)), "job_ref": str(UUID(int=41))}
    if status is ItemStatus.FAILED:
        kwargs["failure"] = "provider_failed"
    with pytest.raises(BatchValidationError):
        ItemOutcome(status, **kwargs)


@pytest.mark.parametrize("failure", ["fail_fast", "interrupted", "cancelled", "report_failed", "project_unavailable", "cache_incomplete", "output_conflict", "provider_failed", "validation_failed"])
def test_typed_failure_codes_are_preserved(failure):
    outcome = ItemOutcome(ItemStatus.FAILED, failure=failure, job_ref=str(UUID(int=40)))
    assert outcome.failure == failure
    assert outcome.job_ref == str(UUID(int=40))


@pytest.mark.parametrize("continuation, expected_statuses, executed_ordinals", [
    (False, [ItemStatus.SUCCESS, ItemStatus.FAILED], [1]),
    (True, [ItemStatus.SUCCESS, ItemStatus.FAILED, ItemStatus.SUCCESS], [1, 3]),
])
def test_planning_exhaustion_is_item_failure_and_respects_continuation(continuation, expected_statuses, executed_ordinals):
    class Plan:
        def __init__(self, failure=None):
            self.planning_failure = failure

    class ExhaustedPlanner:
        def plan(self, command):
            return (Plan(), Plan("auxiliary_allocation_exhausted"), Plan())

    executor = Executor()
    progress = []
    result = use_case(ExhaustedPlanner(), executor, on_progress=progress.append).execute(
        command(3, continue_on_error=continuation))
    assert [item.outcome.status for item in result.items] == expected_statuses
    assert result.items[1].outcome.failure == "auxiliary_allocation_exhausted"
    assert [call[3] for call in executor.calls] == executed_ordinals
    assert progress == list(result.items)
    assert not result.succeeded


def test_interrupted_partial_stops_batch_and_preserves_durable_artifact():
    ref = str(UUID(int=77))

    class InterruptedExecutor(Executor):
        def execute(self, item, plan, correlation_id, ordinal, *, cancellation=None):
            self.calls.append(ordinal)
            return ItemOutcome(ItemStatus.PARTIAL, artifact_ref=ref, interrupted=True)

    executor = InterruptedExecutor()
    result = use_case(Planner(), executor).execute(command(2, continue_on_error=True))
    assert executor.calls == [1]
    assert result.items[0].outcome.artifact_ref == ref
    assert result.items[0].outcome.interrupted
    assert not result.succeeded


def test_invalid_sensitive_reference_does_not_reach_events_or_logs(caplog):
    secret = "/private/sensitive-source.epub"
    events = []
    with caplog.at_level("DEBUG"):
        with pytest.raises(BatchValidationError) as caught:
            ItemOutcome(ItemStatus.SUCCESS, report_ref=secret)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in caplog.text
    assert events == []


def _versioned_batch_values():
    from ayvu.translate_batch import (BatchCancellationCommand, BatchCancellationNotice, BatchFailure,
                                      BatchResult, BatchWarning, StartedItem)

    item = BatchItem(str(UUID(int=1)), str(UUID(int=2)))
    outcome = ItemOutcome(ItemStatus.SUCCESS)
    started = StartedItem(1, str(UUID(int=3)), outcome)
    return (lambda version: BatchItem(item.source_ref, item.output_ref, version=version),
            lambda version: BatchCommand((item,), version=version),
            lambda version: ItemOutcome(ItemStatus.SUCCESS, version=version),
            lambda version: StartedItem(1, str(UUID(int=3)), outcome, version=version),
            lambda version: BatchResult((started,), version=version),
            lambda version: BatchWarning("warning", version=version),
            lambda version: BatchFailure("provider_failed", "provider_transient", version=version),
            lambda version: BatchCancellationCommand(version=version),
            lambda version: BatchCancellationNotice("batch_before_item", version=version))


@pytest.mark.parametrize("build", _versioned_batch_values())
def test_versioned_batch_values_accept_v1_and_reject_other_or_boolean_version(build):
    assert build(1).version == 1
    for version in (2, True):
        with pytest.raises(BatchValidationError, match="unsupported_version"):
            build(version)


@pytest.mark.parametrize("canary", ["documento privado", "senha-secreta", "https://private.example/token", "raw provider response"])
def test_batch_dtos_reject_private_values_without_disclosing_them(canary):
    from ayvu.translate_batch import BatchCancellationCommand, BatchCancellationNotice, BatchFailure, BatchWarning

    builders = (lambda: BatchWarning(canary), lambda: BatchFailure(canary),
                lambda: BatchFailure("provider_failed", family=canary),
                lambda: BatchCancellationCommand(reason_code=canary),
                lambda: BatchCancellationNotice(canary),
                lambda: BatchCancellationNotice("batch_before_item", reason_code=canary))
    for build in builders:
        with pytest.raises(BatchValidationError) as caught:
            build()
        assert canary not in str(caught.value)
        assert canary not in repr(caught.value)


def test_normalized_failure_and_warnings_preserve_codes_and_family():
    from ayvu.translate_batch import BatchFailure, BatchWarning

    outcome = ItemOutcome(ItemStatus.FAILED, warnings=("texts_failed", "warning"), failure="provider_failed",
                          failure_family="provider_transient")
    assert outcome.normalized_failure == BatchFailure("provider_failed", "provider_transient")
    assert outcome.warning_details == (BatchWarning("texts_failed"), BatchWarning("warning"))
    assert ItemOutcome(ItemStatus.SUCCESS).normalized_failure is None
    assert ItemOutcome(ItemStatus.SUCCESS).warning_details == ()


def test_cancellation_controller_accepts_command_and_returns_versioned_notice():
    from ayvu.translate_batch import BatchCancellationCommand, BatchCancellationNotice
    from ayvu.translate_batch_composition import BatchCancellationController

    controller = BatchCancellationController()
    assert controller.checkpoint("batch_before_item") is None
    controller.request(BatchCancellationCommand())
    assert controller.checkpoint("batch_before_item") == BatchCancellationNotice("batch_before_item")
