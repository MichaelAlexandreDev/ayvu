"""The format port, its registry, and the checks that run before any format does work.

Each operation is its own optional port, so a read-only format simply has no
reconstructor. The service decides from the descriptor alone, and it never sees
an output path: an adapter receives its source read-only and returns bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable, Protocol, runtime_checkable

from .artifact import Artifact, ArtifactCancelledError, ArtifactError, ArtifactIntegrityError, ArtifactRequest, ArtifactState
from .artifact_service import ARTIFACT_SAFE_POINTS
from .content import ContentDocument
from .format_adapter import (
    FORMAT_CONTRACT_VERSION, MATERIALIZING_OPERATIONS, MAX_DETECTION_BYTES, AliasVerdict, DetectionEvidence, DetectionOutcome,
    DetectionResult, ExtractionRequest, ExtractionResult, FormatCancelledError, FormatDescriptor, FormatError, FormatInventory,
    FormatOperation, FormatOperationError, FormatRequirements, InspectionResult, MaterializationResult, PreviewRequest,
    ReconstructionRequest, ValidationFinding, ValidationResult, negotiate_format, valid_source_fingerprint,
)
from .job import is_slug
from .job_events import JobFailure, JobFailureFamily
from .job_execution import CancellationToken, JobCancellationError

FORMAT_BEFORE_WORK = "format_before_work"


def _failure(family: JobFailureFamily, code: str) -> FormatOperationError:
    return FormatOperationError(JobFailure(family, code))


class FormatSource(Protocol):
    """A read-only view of the input. Nothing in the contract can write through it."""

    @property
    def extension(self) -> str | None: ...
    def size(self) -> int: ...
    def fingerprint(self) -> str: ...
    def head(self, limit: int) -> bytes: ...
    def open(self) -> BinaryIO: ...


class OutputGuard(Protocol):
    """Answers whether the destination is the source. "Unknown" stops the work as well."""

    def verdict(self, source: FormatSource) -> AliasVerdict: ...


class ArtifactPublisher(Protocol):
    def publish(self, request: ArtifactRequest, chunks: Iterable[bytes], *, owner_token: str,
                cancellation: CancellationToken | None = None) -> Artifact: ...


class FormatContext:
    """What an operation receives: its source, the fingerprint it was checked against, and its safe points."""

    def __init__(self, source: FormatSource, source_fingerprint: str, safe_points: tuple[str, ...],
                 cancellation: CancellationToken | None) -> None:
        self.source = source
        self.source_fingerprint = source_fingerprint
        self._safe_points = frozenset(safe_points)
        self._cancellation = cancellation
        self.cancelled = False

    def __repr__(self) -> str:
        return "FormatContext()"

    def checkpoint(self, safe_point: str) -> None:
        if safe_point not in self._safe_points:
            raise _failure(JobFailureFamily.INTERNAL, "undeclared_safe_point")
        # Remembered, so an adapter that catches the error still cannot return partial work.
        if self._cancellation is not None and self._cancellation.checkpoint(safe_point) is not None:
            self.cancelled = True
            raise FormatCancelledError("cancelled")


@runtime_checkable
class FormatInspector(Protocol):
    def inspect(self, context: FormatContext) -> FormatInventory: ...


@runtime_checkable
class FormatExtractor(Protocol):
    def extract(self, context: FormatContext, request: ExtractionRequest) -> tuple[ContentDocument, ...]: ...


@runtime_checkable
class FormatPreviewer(Protocol):
    def preview(self, context: FormatContext, request: PreviewRequest) -> Iterable[bytes]: ...


@runtime_checkable
class FormatReconstructor(Protocol):
    def reconstruct(self, context: FormatContext, request: ReconstructionRequest) -> Iterable[bytes]: ...


@runtime_checkable
class FormatValidator(Protocol):
    def validate(self, context: FormatContext) -> tuple[ValidationFinding, ...]: ...


_PORTS = {
    FormatOperation.INSPECT: ("inspector", FormatInspector),
    FormatOperation.EXTRACT: ("extractor", FormatExtractor),
    FormatOperation.PREVIEW: ("previewer", FormatPreviewer),
    FormatOperation.RECONSTRUCT: ("reconstructor", FormatReconstructor),
    FormatOperation.VALIDATE: ("validator", FormatValidator),
}


@dataclass(frozen=True, repr=False)
class FormatAdapter:
    """A descriptor and the ports it claims. A claim without a port, or a port without a claim, is refused."""

    descriptor: FormatDescriptor
    inspector: FormatInspector | None = None
    extractor: FormatExtractor | None = None
    previewer: FormatPreviewer | None = None
    reconstructor: FormatReconstructor | None = None
    validator: FormatValidator | None = None

    def __post_init__(self) -> None:
        if type(self.descriptor) is not FormatDescriptor:
            raise FormatError("invalid_adapter")
        for operation, (field, port) in _PORTS.items():
            implementation = getattr(self, field)
            if self.descriptor.runs(operation) != (implementation is not None):
                raise FormatError("descriptor_mismatch")
            if implementation is not None and not isinstance(implementation, port):
                raise FormatError("descriptor_mismatch")

    def __repr__(self) -> str:
        return f"FormatAdapter(descriptor={self.descriptor!r})"


@dataclass(frozen=True)
class FormatRegistration:
    """What the composition root expects, so an upgraded adapter cannot slip in unnoticed."""

    adapter_id: str
    adapter_version: int
    locators: tuple[tuple[str, int], ...]
    contract_version: int = FORMAT_CONTRACT_VERSION


class FormatRegistry:
    """Filled explicitly at a composition root. It imports nothing, so user data cannot load code."""

    def __init__(self) -> None:
        self._adapters: dict[str, FormatAdapter] = {}

    def __repr__(self) -> str:
        return f"FormatRegistry(adapters={len(self._adapters)})"

    def register(self, registration: FormatRegistration, adapter: FormatAdapter) -> None:
        if type(registration) is not FormatRegistration or type(adapter) is not FormatAdapter or not is_slug(registration.adapter_id):
            raise FormatError("invalid_registration")
        descriptor = adapter.descriptor
        if registration.adapter_id in self._adapters:
            raise FormatError("duplicate_adapter")
        if ((descriptor.adapter_id, descriptor.adapter.adapter_version, descriptor.contract_version)
                != (registration.adapter_id, registration.adapter_version, registration.contract_version)
                or descriptor.contract_version != FORMAT_CONTRACT_VERSION):
            raise FormatError("incompatible_adapter")
        if descriptor.locators != registration.locators:
            raise FormatError("incompatible_locator")
        self._adapters[registration.adapter_id] = adapter

    def descriptors(self) -> tuple[FormatDescriptor, ...]:
        return tuple(self._adapters[key].descriptor for key in sorted(self._adapters))

    def get(self, adapter_id: object) -> FormatAdapter:
        adapter = self._adapters.get(adapter_id) if isinstance(adapter_id, str) else None
        if adapter is None:
            raise FormatError("unknown_adapter")
        return adapter

    def for_document(self, document: ContentDocument) -> FormatAdapter:
        adapter = self.get(document.adapter.namespace)
        code = _document_mismatch(adapter.descriptor, document)
        if code is not None:
            raise FormatError(code)
        return adapter

    def detect(self, evidence: DetectionEvidence) -> DetectionResult:
        """Bytes outrank names. Nothing is guessed: a real disagreement comes back as ambiguous."""
        descriptors = self.descriptors()
        strong = [item for item in descriptors if any(signature.matches(evidence.head) for signature in item.signatures)]
        named = [item for item in descriptors if item.recognizes(evidence.extension, evidence.media_type)]
        weak = [item for item in named if not item.signatures]
        if len(strong) > 1:
            return DetectionResult(DetectionOutcome.AMBIGUOUS, "multiple_signatures", _ids(strong))
        if strong:
            if weak and strong[0] not in named:
                return DetectionResult(DetectionOutcome.AMBIGUOUS, "signature_extension_conflict", _ids(strong + weak))
            return DetectionResult(DetectionOutcome.MATCH, "signature_match", _ids(strong))
        if len(weak) > 1:
            return DetectionResult(DetectionOutcome.AMBIGUOUS, "multiple_extensions", _ids(weak))
        if weak:
            return DetectionResult(DetectionOutcome.MATCH, "extension_match", _ids(weak))
        if named:
            return DetectionResult(DetectionOutcome.NO_MATCH, "signature_conflict")
        return DetectionResult(DetectionOutcome.NO_MATCH, "no_candidate")


def _ids(descriptors: list[FormatDescriptor]) -> tuple[str, ...]:
    return tuple(sorted(item.adapter_id for item in descriptors))


def _document_mismatch(descriptor: FormatDescriptor, document: ContentDocument) -> str | None:
    if document.adapter != descriptor.adapter:
        return "incompatible_adapter"
    if any((unit.content.locator.schema, unit.content.locator.schema_version) not in descriptor.locators for unit in document.units):
        return "incompatible_locator"
    return None


def format_safe_points(descriptor: FormatDescriptor) -> tuple[str, ...]:
    """Everything a ``CancellationSource`` must declare to run this adapter's operations."""
    declared = {point for claim in descriptor.operations for point in claim.safe_points}
    return (FORMAT_BEFORE_WORK, *sorted(declared), *ARTIFACT_SAFE_POINTS)


@dataclass(frozen=True, repr=False)
class FormatPublication:
    """Where a result goes. There is no path here, so an adapter cannot be handed its own input."""

    artifacts: ArtifactPublisher
    request: ArtifactRequest
    owner_token: str
    guard: OutputGuard

    def __repr__(self) -> str:
        return f"FormatPublication(request={self.request!r})"


def _cancelled() -> JobFailure:
    return JobFailure(JobFailureFamily.CANCELLED, "cancelled")


def _run(body: Callable[[], object], result: type) -> object:
    """Nothing an adapter raises can pass as a result, and its message is never read."""
    try:
        return body()
    except FormatOperationError as error:
        return result(failure=error.failure)
    except FormatCancelledError:
        return result(failure=_cancelled())
    except JobCancellationError:
        return result(failure=JobFailure(JobFailureFamily.INTERNAL, "undeclared_safe_point"))
    except Exception:
        return result(failure=JobFailure(JobFailureFamily.INTERNAL, "adapter_failed"))


def _read(call: Callable[[], object]) -> object:
    try:
        return call()
    except Exception:
        raise _failure(JobFailureFamily.RESOURCE, "source_unreadable") from None


class FormatService:
    def __init__(self, registry: FormatRegistry) -> None:
        self._registry = registry

    def __repr__(self) -> str:
        return "FormatService()"

    def detect(self, source: FormatSource) -> DetectionResult:
        try:
            evidence = DetectionEvidence(source.head(MAX_DETECTION_BYTES), source.extension)
        except Exception:
            return DetectionResult(DetectionOutcome.NO_MATCH, "source_unreadable")
        return self._registry.detect(evidence)

    def _negotiated(self, adapter_id: object, operation: FormatOperation, requirements: FormatRequirements) -> FormatAdapter:
        try:
            adapter = self._registry.get(adapter_id)
        except FormatError:
            raise _failure(JobFailureFamily.CAPABILITY, "unknown_adapter") from None
        needed = FormatRequirements((operation, *requirements.operations), requirements.fidelity, requirements.scopes,
                                    requirements.allow_experimental)
        negotiation = negotiate_format(adapter.descriptor, needed)
        if not negotiation.accepted:
            raise _failure(JobFailureFamily.CAPABILITY, negotiation.code)
        return adapter

    def _context(self, adapter: FormatAdapter, operation: FormatOperation, source: FormatSource,
                 cancellation: CancellationToken | None) -> FormatContext:
        # Before the hash, which is the expensive part of getting started.
        if cancellation is not None and cancellation.checkpoint(FORMAT_BEFORE_WORK) is not None:
            raise FormatCancelledError("cancelled")
        descriptor = adapter.descriptor
        limit = descriptor.limits.max_source_bytes
        if limit is not None and _read(source.size) > limit:
            raise _failure(JobFailureFamily.RESOURCE, "source_too_large")
        fingerprint = _read(source.fingerprint)
        if not valid_source_fingerprint(fingerprint):
            raise _failure(JobFailureFamily.RESOURCE, "source_unreadable")
        return FormatContext(source, fingerprint, descriptor.claim(operation).safe_points, cancellation)

    @staticmethod
    def _finish(context: FormatContext) -> None:
        if context.cancelled:
            raise FormatCancelledError("cancelled")
        if _read(context.source.fingerprint) != context.source_fingerprint:
            raise _failure(JobFailureFamily.INTEGRITY, "source_changed")

    def inspect(self, adapter_id: str, source: FormatSource, *, allow_experimental: bool = False,
                cancellation: CancellationToken | None = None) -> InspectionResult:
        def body() -> InspectionResult:
            adapter = self._negotiated(adapter_id, FormatOperation.INSPECT, FormatRequirements(allow_experimental=allow_experimental))
            context = self._context(adapter, FormatOperation.INSPECT, source, cancellation)
            inventory = adapter.inspector.inspect(context)
            if type(inventory) is not FormatInventory or (
                    inventory.format_version is not None and inventory.format_version not in adapter.descriptor.format_versions):
                raise _failure(JobFailureFamily.INTERNAL, "descriptor_mismatch")
            self._finish(context)
            return InspectionResult(inventory=inventory)
        return _run(body, InspectionResult)

    def extract(self, adapter_id: str, source: FormatSource, request: ExtractionRequest, *,
                cancellation: CancellationToken | None = None) -> ExtractionResult:
        def body() -> ExtractionResult:
            if type(request) is not ExtractionRequest:
                raise _failure(JobFailureFamily.INPUT, "invalid_extraction_request")
            adapter = self._negotiated(adapter_id, FormatOperation.EXTRACT,
                                       FormatRequirements(scopes=request.scopes, allow_experimental=request.allow_experimental))
            context = self._context(adapter, FormatOperation.EXTRACT, source, cancellation)
            documents = adapter.extractor.extract(context, request)
            self._check_extracted(adapter.descriptor, context, request, documents)
            self._finish(context)
            return ExtractionResult(documents=documents)
        return _run(body, ExtractionResult)

    @staticmethod
    def _check_extracted(descriptor: FormatDescriptor, context: FormatContext, request: ExtractionRequest, documents: object) -> None:
        if type(documents) is not tuple or any(type(item) is not ContentDocument for item in documents):
            raise _failure(JobFailureFamily.INTERNAL, "descriptor_mismatch")
        limit = descriptor.limits.max_documents
        if limit is not None and len(documents) > limit:
            raise _failure(JobFailureFamily.RESOURCE, "too_many_documents")
        if len({item.document_id for item in documents}) != len(documents):
            raise _failure(JobFailureFamily.INTERNAL, "descriptor_mismatch")
        for document in documents:
            if (_document_mismatch(descriptor, document) is not None or document.project_id != request.project_id
                    or document.source_fingerprint != context.source_fingerprint
                    or (request.selection is not None and document.logical_key not in request.selection)):
                raise _failure(JobFailureFamily.INTERNAL, "descriptor_mismatch")

    def validate(self, adapter_id: str, source: FormatSource, *, allow_experimental: bool = False,
                 cancellation: CancellationToken | None = None) -> ValidationResult:
        def body() -> ValidationResult:
            adapter = self._negotiated(adapter_id, FormatOperation.VALIDATE, FormatRequirements(allow_experimental=allow_experimental))
            context = self._context(adapter, FormatOperation.VALIDATE, source, cancellation)
            findings = adapter.validator.validate(context)
            if type(findings) is not tuple or any(type(item) is not ValidationFinding for item in findings):
                raise _failure(JobFailureFamily.INTERNAL, "descriptor_mismatch")
            self._finish(context)
            return ValidationResult(findings=findings)
        return _run(body, ValidationResult)

    def preview(self, adapter_id: str, source: FormatSource, request: PreviewRequest, publication: FormatPublication, *,
                cancellation: CancellationToken | None = None) -> MaterializationResult:
        if type(request) is not PreviewRequest:
            return MaterializationResult(failure=JobFailure(JobFailureFamily.INPUT, "invalid_materialization_request"))
        return self._materialize(adapter_id, FormatOperation.PREVIEW, source, request, publication, cancellation, complete=False)

    def reconstruct(self, adapter_id: str, source: FormatSource, request: ReconstructionRequest, publication: FormatPublication, *,
                    cancellation: CancellationToken | None = None) -> MaterializationResult:
        if type(request) is not ReconstructionRequest:
            return MaterializationResult(failure=JobFailure(JobFailureFamily.INPUT, "invalid_materialization_request"))
        return self._materialize(adapter_id, FormatOperation.RECONSTRUCT, source, request, publication, cancellation, complete=True)

    def _materialize(self, adapter_id: str, operation: FormatOperation, source: FormatSource,
                     request: PreviewRequest | ReconstructionRequest, publication: FormatPublication,
                     cancellation: CancellationToken | None, *, complete: bool) -> MaterializationResult:
        assert operation in MATERIALIZING_OPERATIONS

        def body() -> MaterializationResult:
            if type(publication) is not FormatPublication:
                raise _failure(JobFailureFamily.INPUT, "invalid_publication")
            adapter = self._negotiated(adapter_id, operation, request.requirements)
            self._check_request(adapter.descriptor, request, complete)
            self._check_destination(publication, source)
            context = self._context(adapter, operation, source, cancellation)
            if any(document.source_fingerprint != context.source_fingerprint for document in request.documents):
                raise _failure(JobFailureFamily.INTEGRITY, "source_changed")
            port = adapter.previewer.preview if operation is FormatOperation.PREVIEW else adapter.reconstructor.reconstruct
            return self._publish(publication, context, port, request, cancellation)
        return _run(body, MaterializationResult)

    @staticmethod
    def _check_request(descriptor: FormatDescriptor, request: PreviewRequest | ReconstructionRequest, complete: bool) -> None:
        units: set[str] = set()
        for document in request.documents:
            mismatch = _document_mismatch(descriptor, document)
            if mismatch is not None:
                raise _failure(JobFailureFamily.INTEGRITY, mismatch)
            units.update(unit.unit_id for unit in document.units)
        translated = {item.unit_id for item in request.translations}
        if translated - units:
            raise _failure(JobFailureFamily.INTEGRITY, "unknown_unit")
        if complete and units - translated:
            raise _failure(JobFailureFamily.INTEGRITY, "incomplete_translations")

    @staticmethod
    def _check_destination(publication: FormatPublication, source: FormatSource) -> None:
        try:
            verdict = publication.guard.verdict(source)
        except Exception:
            verdict = AliasVerdict.UNKNOWN
        if verdict is AliasVerdict.SAME:
            raise _failure(JobFailureFamily.POLICY, "output_is_source")
        if verdict is not AliasVerdict.DISTINCT:
            raise _failure(JobFailureFamily.POLICY, "output_alias_unknown")

    def _publish(self, publication: FormatPublication, context: FormatContext, port: Callable[..., Iterable[bytes]],
                 request: PreviewRequest | ReconstructionRequest, cancellation: CancellationToken | None) -> MaterializationResult:
        raised: list[Exception] = []

        def chunks() -> Iterable[bytes]:
            # The store reduces what is raised here to a generic code, so the real reason is kept aside.
            try:
                for chunk in port(context, request):
                    if type(chunk) is not bytes:
                        raise _failure(JobFailureFamily.INTERNAL, "malformed_output")
                    yield chunk
                self._finish(context)
            except Exception as error:
                raised.append(error)
                raise

        try:
            artifact = publication.artifacts.publish(publication.request, chunks(), owner_token=publication.owner_token,
                                                     cancellation=cancellation)
        except ArtifactCancelledError:
            return MaterializationResult(failure=_cancelled())
        except Exception as error:
            if raised:
                raise raised[0] from None
            return MaterializationResult(failure=_publication_failure(error))
        if type(artifact) is not Artifact or artifact.state is not ArtifactState.READY:
            return MaterializationResult(failure=JobFailure(JobFailureFamily.INTERNAL, "publication_failed"))
        return MaterializationResult(artifact=artifact)


def _publication_failure(error: Exception) -> JobFailure:
    code = str(error) if isinstance(error, ArtifactError) and is_slug(str(error)) else "publication_failed"
    family = JobFailureFamily.INTEGRITY if isinstance(error, ArtifactIntegrityError) else JobFailureFamily.RESOURCE
    return JobFailure(family, code)
