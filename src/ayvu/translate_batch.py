"""Sequential, bounded orchestration of independent translation requests.

References and accepted plans belong to the injected boundaries; this layer never
opens a project or interprets an output path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Protocol
from uuid import UUID


class BatchValidationError(ValueError):
    """A bounded, non-sensitive request or planning error code."""


_FAILURE_CODES = frozenset({"translation_failed", "unexpected_error", "provider_failed", "validation_failed",
                            "output_conflict", "auxiliary_allocation_exhausted", "failure", "fail_fast",
                            "interrupted", "cancelled", "report_failed", "project_unavailable", "cache_incomplete",
                            "input_failed", "publication_uncommitted"})
_WARNING_CODES = frozenset({"translation_warnings", "texts_failed", "texts_missing", "validation_warnings",
                            "provenance_failed", "warning", "report_failed", "interrupted_after_publication"})
_FAILURE_FAMILIES = frozenset({"input", "capability", "policy", "provider_transient", "provider_permanent",
                              "resource", "integrity", "cancelled", "internal"})


@dataclass(frozen=True)
class BatchWarning:
    code: str
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if self.code not in _WARNING_CODES:
            raise BatchValidationError("invalid_warning")


@dataclass(frozen=True)
class BatchFailure:
    code: str
    family: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if self.code not in _FAILURE_CODES:
            raise BatchValidationError("invalid_failure")
        if self.family is not None and self.family not in _FAILURE_FAMILIES:
            raise BatchValidationError("invalid_failure_family")


@dataclass(frozen=True)
class BatchCancellationCommand:
    reason_code: str = "batch_cancelled"
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if self.reason_code != "batch_cancelled":
            raise BatchValidationError("invalid_cancellation")


@dataclass(frozen=True)
class BatchCancellationNotice:
    safe_point: str
    reason_code: str = "batch_cancelled"
    version: int = 1

    def __post_init__(self) -> None:
        BatchCancellationCommand(self.reason_code, self.version)
        if self.safe_point not in ("batch_before_item", "dry_run_safe_point"):
            raise BatchValidationError("invalid_cancellation")


class ItemStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BatchItem:
    source_ref: str = field(repr=False)
    output_ref: str = field(repr=False)
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        for ref in (self.source_ref, self.output_ref):
            try:
                valid = isinstance(ref, str) and str(UUID(ref)) == ref
            except (ValueError, AttributeError):
                valid = False
            if not valid:
                raise BatchValidationError("invalid_reference")


@dataclass(frozen=True)
class BatchCommand:
    items: tuple[BatchItem, ...]
    continue_on_error: bool = False
    missing_output: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if not isinstance(self.items, tuple) or not 1 <= len(self.items) <= 100:
            raise BatchValidationError("invalid_item_count")
        if any(type(item) is not BatchItem for item in self.items):
            raise BatchValidationError("invalid_item")
        if type(self.continue_on_error) is not bool or type(self.missing_output) is not bool:
            raise BatchValidationError("invalid_options")
        if len(self.items) > 1 and self.missing_output:
            raise BatchValidationError("ambiguous_missing_output")


@dataclass(frozen=True)
class ItemOutcome:
    status: ItemStatus
    warnings: tuple[str, ...] = ()
    failure: str | None = None
    job_ref: str | None = field(default=None, repr=False)
    artifact_ref: str | None = field(default=None, repr=False)
    report_ref: str | None = field(default=None, repr=False)
    interrupted: bool = False
    job_state: str | None = None
    output_published: bool = False
    failure_family: str | None = None
    version: int = 1

    @property
    def normalized_failure(self) -> BatchFailure | None:
        return None if self.failure is None else BatchFailure(self.failure, self.failure_family, self.version)

    @property
    def warning_details(self) -> tuple[BatchWarning, ...]:
        return tuple(BatchWarning(code, self.version) for code in self.warnings)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if type(self.status) is not ItemStatus or type(self.warnings) is not tuple:
            raise BatchValidationError("invalid_outcome")
        if any(w not in _WARNING_CODES for w in self.warnings):
            raise BatchValidationError("invalid_warning")
        if self.status is ItemStatus.FAILED:
            if self.failure not in _FAILURE_CODES:
                raise BatchValidationError("invalid_failure")
        elif self.failure is not None:
            raise BatchValidationError("invalid_failure")
        for ref in (self.job_ref, self.artifact_ref, self.report_ref):
            if ref is not None:
                try:
                    valid = isinstance(ref, str) and str(UUID(ref)) == ref
                except (ValueError, AttributeError):
                    valid = False
                if not valid:
                    raise BatchValidationError("invalid_reference")
        if self.status in (ItemStatus.FAILED, ItemStatus.CANCELLED) and (self.artifact_ref or self.report_ref):
            raise BatchValidationError("invalid_publication_reference")
        if type(self.interrupted) is not bool or type(self.output_published) is not bool:
            raise BatchValidationError("invalid_outcome")
        if self.job_state not in (None, "queued", "preflight", "running", "completed", "validating", "validated",
                                   "partially_completed", "failed", "cancelling", "cancelled"):
            raise BatchValidationError("invalid_job_state")
        if self.failure_family is not None and self.failure_family not in _FAILURE_FAMILIES:
            raise BatchValidationError("invalid_failure_family")


@dataclass(frozen=True)
class StartedItem:
    ordinal: int
    correlation_id: str
    outcome: ItemOutcome
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 100 or type(self.outcome) is not ItemOutcome:
            raise BatchValidationError("invalid_progress")
        try:
            valid = isinstance(self.correlation_id, str) and str(UUID(self.correlation_id)) == self.correlation_id
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise BatchValidationError("invalid_correlation")


@dataclass(frozen=True)
class BatchResult:
    items: tuple[StartedItem, ...]
    cancelled: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BatchValidationError("unsupported_version")
        if (type(self.items) is not tuple or len(self.items) > 100 or type(self.cancelled) is not bool
                or any(type(item) is not StartedItem or item.ordinal != ordinal for ordinal, item in enumerate(self.items, 1))):
            raise BatchValidationError("invalid_result")

    @property
    def succeeded(self) -> bool:
        return not self.cancelled and all(i.outcome.status in (ItemStatus.SUCCESS, ItemStatus.PARTIAL) for i in self.items)


class BatchPlanner(Protocol):
    def plan(self, command: BatchCommand) -> tuple[object, ...]: ...


class ItemExecutor(Protocol):
    def execute(self, item: BatchItem, plan: object, correlation_id: str, ordinal: int,
                *, cancellation: BatchCancellation | None = None) -> ItemOutcome: ...


class BatchCancellation(Protocol):
    def checkpoint(self, safe_point: str) -> object | None: ...


class TranslateBatch:
    def __init__(self, planner: BatchPlanner, executor: ItemExecutor, new_id: Callable[[], str],
                 cancellation: BatchCancellation | None = None,
                 on_progress: Callable[[StartedItem], None] | None = None) -> None:
        self._planner = planner
        self._executor = executor
        self._new_id = new_id
        self._cancellation = cancellation
        self._on_progress = on_progress

    def execute(self, command: BatchCommand) -> BatchResult:
        if type(command) is not BatchCommand:
            raise BatchValidationError("invalid_command")
        plans = self._planner.plan(command)
        if len(plans) != len(command.items):
            raise BatchValidationError("invalid_plan")
        results: list[StartedItem] = []
        ids: set[str] = set()
        for ordinal, (item, plan) in enumerate(zip(command.items, plans), 1):
            if self._cancellation and self._cancellation.checkpoint("batch_before_item") is not None:
                return BatchResult(tuple(results), cancelled=True)
            correlation = self._new_id()
            try:
                valid_id = isinstance(correlation, str) and str(UUID(correlation)) == correlation
            except (ValueError, AttributeError):
                valid_id = False
            if not valid_id or correlation in ids:
                raise BatchValidationError("invalid_correlation")
            ids.add(correlation)
            planning_failure = getattr(plan, "planning_failure", None)
            outcome = (ItemOutcome(ItemStatus.FAILED, failure=planning_failure) if planning_failure else
                       self._executor.execute(item, plan, correlation, ordinal, cancellation=self._cancellation))
            if type(outcome) is not ItemOutcome:
                raise BatchValidationError("invalid_outcome")
            started = StartedItem(ordinal, correlation, outcome)
            results.append(started)
            if self._on_progress:
                self._on_progress(started)
            if outcome.status is ItemStatus.CANCELLED or outcome.interrupted:
                return BatchResult(tuple(results), cancelled=True)
            if outcome.status is ItemStatus.FAILED and not command.continue_on_error:
                break
        return BatchResult(tuple(results))
