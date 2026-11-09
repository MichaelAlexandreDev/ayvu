"""Framework-neutral read inspection and visible-content export use cases.

The format service owns detection and format parsing. A writer port owns local
destination planning and atomic publication; this module coordinates their
typed outcomes without learning EPUB, Markdown, or filesystem rules.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .format_adapter import (
    DetectionOutcome, DetectionResult, FormatDescriptor, FormatInventory, VisibleContentDocument, valid_source_fingerprint,
)
from .format_service import FormatService, FormatSource
from .job import is_slug
from .job_events import JobFailure, JobFailureFamily, JobWarning
from .job_execution import CancellationToken, JobCancellationError

CONTENT_BEFORE_INSPECTION = "content_before_inspection"
CONTENT_BEFORE_EXPORT = "content_before_export"
CONTENT_BETWEEN_DOCUMENTS = "content_between_documents"
CONTENT_BEFORE_PUBLICATION = "content_before_publication"
CONTENT_SAFE_POINTS = (
    CONTENT_BEFORE_INSPECTION,
    CONTENT_BEFORE_EXPORT,
    CONTENT_BETWEEN_DOCUMENTS,
    CONTENT_BEFORE_PUBLICATION,
)


class ContentUseCaseError(ValueError):
    """A bounded validation code. Values and paths are never part of its text."""


@dataclass(frozen=True, repr=False)
class SourceRef:
    value: str

    def __post_init__(self) -> None:
        if not is_slug(self.value):
            raise ContentUseCaseError("invalid_source_ref")

    def __repr__(self) -> str:
        return "SourceRef()"


@dataclass(frozen=True, repr=False)
class SourceRevision:
    value: str

    def __post_init__(self) -> None:
        if not valid_source_fingerprint(self.value):
            raise ContentUseCaseError("invalid_source_revision")

    def __repr__(self) -> str:
        return "SourceRevision()"


@dataclass(frozen=True, repr=False)
class OutputDirectoryRef:
    """An explicitly selected destination, opaque in default representations."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ContentUseCaseError("invalid_output_directory")

    def __repr__(self) -> str:
        return "OutputDirectoryRef()"


class ProgressSink(Protocol):
    def report(self, progress: "ContentProgress") -> None: ...


@dataclass(frozen=True)
class ContentProgress:
    phase: str
    completed: int
    total: int | None

    def __post_init__(self) -> None:
        if (not is_slug(self.phase) or type(self.completed) is not int or self.completed < 0
                or (self.total is not None and (type(self.total) is not int or self.total < 0))
                or (self.total is not None and self.completed > self.total)):
            raise ContentUseCaseError("invalid_content_progress")


@dataclass(frozen=True, repr=False)
class InspectContentRequest:
    source_ref: SourceRef
    source: FormatSource
    allow_experimental: bool = False
    cancellation: CancellationToken | None = None
    progress: ProgressSink | None = None

    def __post_init__(self) -> None:
        if (type(self.source_ref) is not SourceRef or not isinstance(self.allow_experimental, bool)
                or (self.cancellation is not None and not isinstance(self.cancellation, CancellationToken))):
            raise ContentUseCaseError("invalid_inspect_request")

    def __repr__(self) -> str:
        return f"InspectContentRequest(source_ref={self.source_ref!r})"


@dataclass(frozen=True, repr=False)
class InspectContentResult:
    source_ref: SourceRef
    source_revision: SourceRevision | None = None
    format_descriptor: FormatDescriptor | None = None
    inventory: FormatInventory | None = None
    warnings: tuple[JobWarning, ...] = ()
    progress: ContentProgress | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        success = self.inventory is not None
        if (type(self.source_ref) is not SourceRef or success != (self.failure is None)
                or (success and (type(self.source_revision) is not SourceRevision
                                 or type(self.format_descriptor) is not FormatDescriptor))
                or (not success and self.source_revision is not None)
                or (not success and self.format_descriptor is not None and type(self.format_descriptor) is not FormatDescriptor)
                or (self.failure is not None and type(self.failure) is not JobFailure)
                or type(self.warnings) is not tuple or any(type(item) is not JobWarning for item in self.warnings)):
            raise ContentUseCaseError("invalid_inspect_result")

    @property
    def title(self) -> str | None:
        return None if self.inventory is None else self.inventory.title

    @property
    def authors(self) -> tuple[str, ...]:
        return () if self.inventory is None else self.inventory.authors

    @property
    def language(self) -> str | None:
        return None if self.inventory is None else self.inventory.language

    @property
    def document_count(self) -> int | None:
        if self.inventory is None:
            return None
        return (self.inventory.source_document_count if self.inventory.source_document_count is not None
                else self.inventory.document_count)

    @property
    def item_count(self) -> int | None:
        return None if self.inventory is None else self.inventory.resource_count

    @property
    def format_version(self) -> str | None:
        return None if self.inventory is None else self.inventory.format_version

    @property
    def capabilities(self) -> tuple[str, ...]:
        if self.format_descriptor is None:
            return ()
        return tuple(claim.operation.value for claim in self.format_descriptor.operations
                     if self.format_descriptor.runs(claim.operation))

    def __repr__(self) -> str:
        if self.failure is not None:
            return f"InspectContentResult(failure={self.failure!r})"
        return "InspectContentResult(success)"


def _adapter_to_try(detected: DetectionResult) -> str | None:
    """A damaged signature proves no other format: the one adapter that owns the extension opens it or says why not."""
    if detected.outcome is DetectionOutcome.MATCH:
        return detected.adapter_id
    if detected.reason == "signature_conflict" and len(detected.candidates) == 1:
        return detected.candidates[0]
    return None


class InspectContent:
    def __init__(self, formats: FormatService) -> None:
        self._formats = formats

    def safe_points(self, adapter_id: str | None = None) -> tuple[str, ...]:
        declared = () if adapter_id is None else self._formats.safe_points(adapter_id)
        return tuple(dict.fromkeys((*CONTENT_SAFE_POINTS, *declared)))

    def execute(self, request: InspectContentRequest) -> InspectContentResult:
        progress: ContentProgress | None = None
        if not _report(request.progress, ContentProgress("inspection", 0, 1)):
            return _inspect_failure(request, _failure(JobFailureFamily.INTERNAL, "progress_sink_failed"))
        progress = ContentProgress("inspection", 0, 1)
        checkpoint = _checkpoint(request.cancellation, CONTENT_BEFORE_INSPECTION)
        if checkpoint is not None:
            return _inspect_failure(request, checkpoint, progress)
        detected = self._formats.detect(request.source)
        adapter_id = _adapter_to_try(detected)
        if adapter_id is None:
            code = "ambiguous_format" if detected.outcome is DetectionOutcome.AMBIGUOUS else "unsupported_format"
            family = JobFailureFamily.CAPABILITY
            return _inspect_failure(request, _failure(family, code), progress)
        try:
            descriptor = self._formats.descriptor(adapter_id)
        except Exception:
            return _inspect_failure(request, _failure(JobFailureFamily.CAPABILITY, "unknown_adapter"), progress)
        result = self._formats.inspect(adapter_id, request.source,
                                       allow_experimental=request.allow_experimental,
                                       cancellation=request.cancellation)
        if result.failure is not None:
            return _inspect_failure(request, result.failure, progress, descriptor)
        if result.inventory is None or result.source_fingerprint is None:
            return _inspect_failure(request, _failure(JobFailureFamily.INTERNAL, "invalid_inspection_result"), progress,
                                    descriptor)
        progress = ContentProgress("inspection", 1, 1)
        if not _report(request.progress, progress):
            return _inspect_failure(request, _failure(JobFailureFamily.INTERNAL, "progress_sink_failed"), progress,
                                    descriptor)
        return InspectContentResult(request.source_ref, SourceRevision(result.source_fingerprint), descriptor,
                                    result.inventory, progress=progress)


class ExportFileState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_STARTED = "not_started"


@dataclass(frozen=True, repr=False)
class PublishedFileRef:
    ordinal: int
    name: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1 or not isinstance(self.name, str) or not self.name:
            raise ContentUseCaseError("invalid_published_file_ref")

    def __repr__(self) -> str:
        return f"PublishedFileRef(ordinal={self.ordinal})"


@dataclass(frozen=True, repr=False)
class ExportFileOutcome:
    ordinal: int
    state: ExportFileState
    published_file: PublishedFileRef | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if (type(self.ordinal) is not int or self.ordinal < 1 or type(self.state) is not ExportFileState
                or (self.state is ExportFileState.COMPLETED and (self.published_file is None or self.failure is not None))
                or (self.state is ExportFileState.FAILED and (self.published_file is not None or self.failure is None))
                or (self.state is ExportFileState.NOT_STARTED and (self.published_file is not None or self.failure is not None))):
            raise ContentUseCaseError("invalid_export_file_outcome")

    def __repr__(self) -> str:
        suffix = f", failure={self.failure.code!r}" if self.failure is not None else ""
        return f"ExportFileOutcome(ordinal={self.ordinal}, state={self.state.value!r}{suffix})"


@dataclass(frozen=True, repr=False)
class WriterAuthorization:
    token: object | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if (self.failure is not None and type(self.failure) is not JobFailure) or ((self.token is None) == (self.failure is None)):
            raise ContentUseCaseError("invalid_writer_authorization")

    def __repr__(self) -> str:
        return "WriterAuthorization(authorized)" if self.failure is None else f"WriterAuthorization(failure={self.failure!r})"


@dataclass(frozen=True, repr=False)
class PlannedOutputFile:
    ordinal: int
    token: object

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1 or self.token is None:
            raise ContentUseCaseError("invalid_planned_output_file")

    def __repr__(self) -> str:
        return f"PlannedOutputFile(ordinal={self.ordinal})"


@dataclass(frozen=True, repr=False)
class WriterPlan:
    files: tuple[PlannedOutputFile, ...] | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if ((self.files is None) == (self.failure is None)
                or (self.files is not None and (type(self.files) is not tuple
                    or any(type(item) is not PlannedOutputFile for item in self.files)))):
            raise ContentUseCaseError("invalid_writer_plan")

    def __repr__(self) -> str:
        if self.failure is not None:
            return f"WriterPlan(failure={self.failure!r})"
        return f"WriterPlan(files={len(self.files or ())})"


@dataclass(frozen=True, repr=False)
class WriterPublication:
    published_file: PublishedFileRef | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if ((self.published_file is None) == (self.failure is None)
                or (self.failure is not None and type(self.failure) is not JobFailure)
                or (self.published_file is not None and type(self.published_file) is not PublishedFileRef)):
            raise ContentUseCaseError("invalid_writer_publication")

    def __repr__(self) -> str:
        return ("WriterPublication(published)" if self.published_file is not None
                else f"WriterPublication(failure={self.failure!r})")


class VisibleContentWriter(Protocol):
    def authorize(self, destination: OutputDirectoryRef, *, overwrite: bool) -> WriterAuthorization: ...
    def plan(self, authorization: WriterAuthorization, source: FormatSource,
             documents: tuple[VisibleContentDocument, ...]) -> WriterPlan: ...
    def publish(self, authorization: WriterAuthorization, planned: PlannedOutputFile,
                document: VisibleContentDocument, source: FormatSource, *,
                before_publish: Callable[[], JobFailure | None]) -> WriterPublication: ...
    def prepare_empty(self, authorization: WriterAuthorization) -> JobFailure | None: ...


@dataclass(frozen=True, repr=False)
class ExportVisibleContentRequest:
    source_ref: SourceRef
    source: FormatSource
    source_revision: SourceRevision | None
    destination: OutputDirectoryRef
    overwrite: bool = False
    allow_experimental: bool = False
    cancellation: CancellationToken | None = None
    progress: ProgressSink | None = None

    def __post_init__(self) -> None:
        if (type(self.source_ref) is not SourceRef
                or (self.source_revision is not None and type(self.source_revision) is not SourceRevision)
                or type(self.destination) is not OutputDirectoryRef or type(self.overwrite) is not bool
                or type(self.allow_experimental) is not bool
                or (self.cancellation is not None and not isinstance(self.cancellation, CancellationToken))):
            raise ContentUseCaseError("invalid_export_request")

    def __repr__(self) -> str:
        return f"ExportVisibleContentRequest(source_ref={self.source_ref!r}, overwrite={self.overwrite})"


@dataclass(frozen=True, repr=False)
class ExportVisibleContentResult:
    source_ref: SourceRef
    source_revision: SourceRevision | None
    format_descriptor: FormatDescriptor | None = None
    outcomes: tuple[ExportFileOutcome, ...] = ()
    warnings: tuple[JobWarning, ...] = ()
    progress: ContentProgress | None = None
    failure: JobFailure | None = None
    partial_publication: bool = False

    def __post_init__(self) -> None:
        success = self.failure is None
        if (type(self.source_ref) is not SourceRef
                or (success and type(self.source_revision) is not SourceRevision)
                or (self.source_revision is not None and type(self.source_revision) is not SourceRevision)
                or (self.format_descriptor is not None and type(self.format_descriptor) is not FormatDescriptor)
                or type(self.outcomes) is not tuple or any(type(item) is not ExportFileOutcome for item in self.outcomes)
                or type(self.warnings) is not tuple or any(type(item) is not JobWarning for item in self.warnings)
                or (self.failure is not None and type(self.failure) is not JobFailure)
                or type(self.partial_publication) is not bool
                or self.partial_publication != (self.failure is not None and any(item.state is ExportFileState.COMPLETED
                                                                                    for item in self.outcomes))):
            raise ContentUseCaseError("invalid_export_result")

    @property
    def completed_count(self) -> int:
        return sum(item.state is ExportFileState.COMPLETED for item in self.outcomes)

    @property
    def failed_count(self) -> int:
        return sum(item.state is ExportFileState.FAILED for item in self.outcomes)

    @property
    def not_started_count(self) -> int:
        return sum(item.state is ExportFileState.NOT_STARTED for item in self.outcomes)

    def __repr__(self) -> str:
        return (f"ExportVisibleContentResult(completed={self.completed_count}, failed={self.failed_count}, "
                f"not_started={self.not_started_count}, partial={self.partial_publication})")


class ExportVisibleContent:
    def __init__(self, formats: FormatService, writer: VisibleContentWriter) -> None:
        self._formats = formats
        self._writer = writer

    def safe_points(self, adapter_id: str | None = None) -> tuple[str, ...]:
        declared = () if adapter_id is None else self._formats.safe_points(adapter_id)
        return tuple(dict.fromkeys((*CONTENT_SAFE_POINTS, *declared)))

    def execute(self, request: ExportVisibleContentRequest) -> ExportVisibleContentResult:
        progress = ContentProgress("planning", 0, None)
        if not _report(request.progress, progress):
            return _export_failure(request, _failure(JobFailureFamily.INTERNAL, "progress_sink_failed"), progress=progress)
        cancellation = _checkpoint(request.cancellation, CONTENT_BEFORE_EXPORT)
        if cancellation is not None:
            return _export_failure(request, cancellation, progress=progress)
        try:
            authorization = self._writer.authorize(request.destination, overwrite=request.overwrite)
        except Exception:
            return _export_failure(request, _failure(JobFailureFamily.RESOURCE, "output_authorization_failed"), progress=progress)
        if type(authorization) is not WriterAuthorization:
            return _export_failure(request, _failure(JobFailureFamily.INTERNAL, "invalid_writer_authorization"), progress=progress)
        if authorization.failure is not None:
            return _export_failure(request, authorization.failure, progress=progress)

        detected = self._formats.detect(request.source)
        adapter_id = _adapter_to_try(detected)
        if adapter_id is None:
            code = "ambiguous_format" if detected.outcome is DetectionOutcome.AMBIGUOUS else "unsupported_format"
            return _export_failure(request, _failure(JobFailureFamily.CAPABILITY, code), progress=progress)
        try:
            descriptor = self._formats.descriptor(adapter_id)
        except Exception:
            return _export_failure(request, _failure(JobFailureFamily.CAPABILITY, "unknown_adapter"), progress=progress)

        extracted = self._formats.export_visible_content(adapter_id, request.source,
                                                          allow_experimental=request.allow_experimental,
                                                          cancellation=request.cancellation)
        if extracted.failure is not None:
            return _export_failure(request, extracted.failure, descriptor, progress)
        if extracted.documents is None or extracted.source_fingerprint is None:
            return _export_failure(request, _failure(JobFailureFamily.INTERNAL, "invalid_visible_content"), descriptor, progress)
        revision = SourceRevision(extracted.source_fingerprint)
        if request.source_revision is not None and extracted.source_fingerprint != request.source_revision.value:
            return _export_failure(request, _failure(JobFailureFamily.INTEGRITY, "source_changed"), descriptor, progress,
                                   revision)
        documents = extracted.documents
        progress = ContentProgress("planning", 0, len(documents))
        if not _report(request.progress, progress):
            return _export_failure(request, _failure(JobFailureFamily.INTERNAL, "progress_sink_failed"), descriptor, progress)

        try:
            plan = self._writer.plan(authorization, request.source, documents)
        except Exception:
            return _export_not_started(request, documents, _failure(JobFailureFamily.RESOURCE, "output_plan_failed"),
                                        descriptor, progress, revision)
        if type(plan) is not WriterPlan:
            return _export_not_started(request, documents, _failure(JobFailureFamily.INTERNAL, "invalid_writer_plan"),
                                        descriptor, progress, revision)
        if plan.failure is not None:
            return _export_not_started(request, documents, plan.failure, descriptor, progress, revision)
        if plan.files is None or len(plan.files) != len(documents) or tuple(item.ordinal for item in plan.files) != tuple(range(1, len(documents) + 1)):
            return _export_not_started(request, documents, _failure(JobFailureFamily.INTERNAL, "invalid_writer_plan"),
                                        descriptor, progress, revision)
        if not documents:
            try:
                failure = self._writer.prepare_empty(authorization)
            except Exception:
                failure = _failure(JobFailureFamily.RESOURCE, "output_prepare_failed")
            if failure is not None:
                return _export_failure(request, failure, descriptor, progress)
            progress = ContentProgress("writing", 0, 0)
            _report(request.progress, progress)
            return ExportVisibleContentResult(request.source_ref, revision, descriptor, progress=progress)

        outcomes: list[ExportFileOutcome] = []
        failure: JobFailure | None = None
        total = len(documents)
        try:
            source_size = request.source.size()
        except Exception:
            return _export_not_started(request, documents, _failure(JobFailureFamily.RESOURCE, "source_unreadable"),
                                        descriptor, progress, revision)
        for index, (document, planned) in enumerate(zip(documents, plan.files, strict=True), start=1):
            cancellation = _checkpoint(request.cancellation, CONTENT_BETWEEN_DOCUMENTS)
            if cancellation is not None:
                failure = cancellation
                outcomes.extend(_not_started(index, total))
                break
            progress = ContentProgress("writing", index - 1, total)
            if not _report(request.progress, progress):
                failure = _failure(JobFailureFamily.INTERNAL, "progress_sink_failed")
                outcomes.extend(_not_started(index, total))
                break

            def before_publish() -> JobFailure | None:
                stopped = _checkpoint(request.cancellation, CONTENT_BEFORE_PUBLICATION)
                if stopped is not None:
                    return stopped
                # Hashing the whole book before each file made a large book 40 times slower. The first and the
                # last publication hash it; those between compare its size, so a same-size change is caught by the last.
                try:
                    if index not in (1, total):
                        return None if request.source.size() == source_size else _failure(
                            JobFailureFamily.INTEGRITY, "source_changed")
                    current = request.source.fingerprint()
                except Exception:
                    return _failure(JobFailureFamily.RESOURCE, "source_unreadable")
                if not valid_source_fingerprint(current):
                    return _failure(JobFailureFamily.RESOURCE, "source_unreadable")
                if current != revision.value:
                    return _failure(JobFailureFamily.INTEGRITY, "source_changed")
                return None

            try:
                publication = self._writer.publish(authorization, planned, document, request.source,
                                                   before_publish=before_publish)
            except Exception:
                publication = WriterPublication(failure=_failure(JobFailureFamily.RESOURCE, "output_write_failed"))
            if type(publication) is not WriterPublication:
                failure = _failure(JobFailureFamily.INTERNAL, "invalid_writer_publication")
                outcomes.append(ExportFileOutcome(index, ExportFileState.FAILED, failure=failure))
                outcomes.extend(_not_started(index + 1, total))
                break
            if publication.failure is not None:
                failure = publication.failure
                outcomes.append(ExportFileOutcome(index, ExportFileState.FAILED, failure=failure))
                outcomes.extend(_not_started(index + 1, total))
                break
            if publication.published_file is None or publication.published_file.ordinal != index:
                failure = _failure(JobFailureFamily.INTERNAL, "invalid_writer_publication")
                outcomes.append(ExportFileOutcome(index, ExportFileState.FAILED, failure=failure))
                outcomes.extend(_not_started(index + 1, total))
                break
            outcomes.append(ExportFileOutcome(index, ExportFileState.COMPLETED, published_file=publication.published_file))
            progress = ContentProgress("writing", index, total)
            if not _report(request.progress, progress):
                failure = _failure(JobFailureFamily.INTERNAL, "progress_sink_failed")
                outcomes.extend(_not_started(index + 1, total))
                break

        partial = failure is not None and any(item.state is ExportFileState.COMPLETED for item in outcomes)
        warnings = (JobWarning("partial_publication"),) if partial else ()
        return ExportVisibleContentResult(request.source_ref, revision, descriptor, tuple(outcomes), warnings,
                                          progress, failure, partial)


def _failure(family: JobFailureFamily, code: str) -> JobFailure:
    return JobFailure(family, code)


def _checkpoint(token: CancellationToken | None, safe_point: str) -> JobFailure | None:
    if token is None:
        return None
    try:
        return (_failure(JobFailureFamily.CANCELLED, "cancelled")
                if token.checkpoint(safe_point) is not None else None)
    except JobCancellationError:
        return _failure(JobFailureFamily.INTERNAL, "undeclared_safe_point")
    except Exception:
        return _failure(JobFailureFamily.INTERNAL, "cancellation_check_failed")


def _report(sink: ProgressSink | None, progress: ContentProgress) -> bool:
    if sink is None:
        return True
    try:
        sink.report(progress)
        return True
    except Exception:
        return False


def _inspect_failure(request: InspectContentRequest, failure: JobFailure, progress: ContentProgress | None = None,
                     descriptor: FormatDescriptor | None = None) -> InspectContentResult:
    return InspectContentResult(request.source_ref, format_descriptor=descriptor, progress=progress, failure=failure)


def _export_failure(request: ExportVisibleContentRequest, failure: JobFailure, descriptor: FormatDescriptor | None = None,
                    progress: ContentProgress | None = None,
                    source_revision: SourceRevision | None = None) -> ExportVisibleContentResult:
    return ExportVisibleContentResult(request.source_ref, source_revision, descriptor, progress=progress, failure=failure)


def _not_started(start: int, total: int) -> list[ExportFileOutcome]:
    return [ExportFileOutcome(ordinal, ExportFileState.NOT_STARTED) for ordinal in range(start, total + 1)]


def _export_not_started(request: ExportVisibleContentRequest, documents: tuple[VisibleContentDocument, ...],
                        failure: JobFailure, descriptor: FormatDescriptor, progress: ContentProgress | None,
                        source_revision: SourceRevision) -> ExportVisibleContentResult:
    outcomes = tuple(ExportFileOutcome(index, ExportFileState.NOT_STARTED)
                     for index in range(1, len(documents) + 1))
    return ExportVisibleContentResult(request.source_ref, source_revision, descriptor, outcomes,
                                      progress=progress, failure=failure)
