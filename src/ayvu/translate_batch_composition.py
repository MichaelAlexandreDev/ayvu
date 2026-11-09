"""Local batch composition, reusable by clients that supply their own presentation.

Paths and callbacks stay behind opaque request references. Constructing a service
does no I/O; its shared output policy plans the complete batch before any callback.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from .batch_output_policy import AcceptedItem, BatchOutputPolicy, OutputIntent
from .job import JobState
from .job_events import CancellationRequest, JobFailureFamily
from .job_execution import CancellationSource
from .translate_batch import (BatchCommand, BatchItem, BatchValidationError, ItemOutcome, ItemStatus, TranslateBatch,
                              BatchCancellationCommand, BatchCancellationNotice)
from .translate_project import TranslateProjectResult


class BatchCancellationController:
    """One batch request, with a fresh cancellation lifecycle for each active job."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._lock = Lock()
        self._requested: CancellationRequest | None = None
        self._active: CancellationSource | None = None

    def __repr__(self) -> str:
        return "BatchCancellationController()"

    def request(self, command: BatchCancellationCommand = BatchCancellationCommand()) -> None:
        if type(command) is not BatchCancellationCommand:
            raise BatchValidationError("invalid_cancellation")
        with self._lock:
            if self._requested is None:
                self._requested = CancellationRequest(command.reason_code, self._clock())
            if self._active is not None:
                self._active.request(self._requested.reason_code, self._requested.requested_at)

    def checkpoint(self, safe_point: str) -> BatchCancellationNotice | None:
        with self._lock:
            if self._requested is None:
                return None
            return BatchCancellationNotice("dry_run_safe_point" if safe_point == "dry_run_safe_point" else "batch_before_item")

    @contextmanager
    def bind(self, source: CancellationSource) -> Iterator[CancellationSource]:
        with self._lock:
            if self._active is not None:
                raise BatchValidationError("overlapping_items")
            self._active = source
            if self._requested is not None:
                source.request(self._requested.reason_code, self._requested.requested_at)
        try:
            yield source
        finally:
            with self._lock:
                self._active = None


def project_item_outcome(result: TranslateProjectResult, *, report_saved: bool = False,
                         report_failed: bool = False, interrupted: bool = False,
                         fail_fast: bool = False) -> ItemOutcome:
    """Project only durable facts; report I/O failure never rewrites the job outcome.

The report reference identifies the project result from which Markdown is rendered,
and is exposed only after that rendering was saved successfully.
"""
    facts = dict(job_ref=result.job_id, job_state=None if result.state is None else result.state.value,
                 output_published=result.output_ready, interrupted=interrupted,
                 failure_family=None if result.failure is None else result.failure.family.value)
    if result.state is JobState.CANCELLED:
        return ItemOutcome(ItemStatus.CANCELLED, **facts)
    if report_failed:
        return ItemOutcome(ItemStatus.FAILED, failure="report_failed", **facts)
    failure = result.failure
    if interrupted and not result.output_ready:
        return ItemOutcome(ItemStatus.FAILED, failure="interrupted", **facts)
    if failure is not None or not result.recorded or result.state is JobState.FAILED:
        code = "translation_failed"
        if failure is not None:
            if failure.code in ("output_exists", "destination_changed", "source_changed", "source_collision", "destination_collision"):
                code = "output_conflict"
            elif failure.family in (JobFailureFamily.PROVIDER_TRANSIENT, JobFailureFamily.PROVIDER_PERMANENT):
                code = "fail_fast" if fail_fast else "provider_failed"
            elif failure.code == "preflight_failed":
                code = "provider_failed"
            elif failure.code in ("validation_failed", "validation_unavailable"):
                code = "validation_failed"
            elif failure.family is JobFailureFamily.INPUT:
                code = "input_failed"
        return ItemOutcome(ItemStatus.FAILED, failure=code, **facts)
    if result.validation is not None and not result.validation.ok:
        return ItemOutcome(ItemStatus.FAILED, failure="validation_failed", **facts)
    if not result.output_ready:
        reason = None if result.outcome is None else result.outcome.summary.reason_code
        return ItemOutcome(ItemStatus.FAILED, failure="publication_uncommitted" if reason == "publication_uncommitted" else "cache_incomplete", **facts)
    warnings = []
    if result.engine.stats.errors:
        warnings.append("texts_failed")
    if result.engine.stats.missing:
        warnings.append("texts_missing")
    if result.validation is not None and result.validation.warnings:
        warnings.append("validation_warnings")
    if result.outcome is not None and result.outcome.summary.reason_code == "provenance_failed":
        warnings.append("provenance_failed")
    if interrupted:
        warnings.append("interrupted_after_publication")
    status = ItemStatus.PARTIAL if warnings or result.state is JobState.PARTIALLY_COMPLETED else ItemStatus.SUCCESS
    return ItemOutcome(status, warnings=tuple(warnings), artifact_ref=result.artifact.artifact_id,
                       report_ref=result.report_ref if report_saved else None, **facts)


@dataclass(frozen=True, repr=False)
class LocalBatchItem:
    intent: OutputIntent
    run: Callable[[AcceptedItem, str, int, object], ItemOutcome] = field(repr=False)


class LocalTranslateBatch:
    """Production binding of opaque references, complete planning and sequential execution.

An item callback owns source-language/project resolution and presentation. Real
items call translate_in_project, dry runs call translate_without_project. Both
receive the same accepted revision, correlation and cancellation boundary.
"""

    def __init__(self, items: tuple[LocalBatchItem, ...], *, continue_on_error: bool = False,
                 missing_output: bool = False, policy: BatchOutputPolicy | None = None,
                 cancellation: BatchCancellationController | None = None,
                 on_progress: Callable | None = None) -> None:
        self.command = BatchCommand(tuple(BatchItem(str(uuid4()), str(uuid4())) for _ in items),
                                    continue_on_error=continue_on_error, missing_output=missing_output)
        self._bindings = dict(zip(self.command.items, items))
        self._policy = policy or BatchOutputPolicy()
        self.cancellation = cancellation or BatchCancellationController()
        self.use_case = TranslateBatch(self, self, lambda: str(uuid4()), self.cancellation, on_progress)

    def __repr__(self) -> str:
        return "LocalTranslateBatch()"

    def plan(self, command: BatchCommand) -> tuple[AcceptedItem, ...]:
        if command != self.command:
            raise BatchValidationError("invalid_command")
        return self._policy.plan(tuple(self._bindings[item].intent for item in command.items)).items

    def execute(self, item: BatchItem, plan: AcceptedItem, correlation_id: str, ordinal: int,
                *, cancellation=None) -> ItemOutcome:
        return self._bindings[item].run(plan, correlation_id, ordinal, cancellation)
