"""What a format promises, and the values its operations exchange.

Nothing here reads a file or knows a format. A consumer decides only from a
``FormatDescriptor``, never from an adapter's name, a file extension or which
methods happen to exist. Source text, titles and paths stay out of ``repr``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .artifact import Artifact
from .content import UNKNOWN_LOCALE, AdapterIdentity, ContentDocument, SourceLocale
from .job import is_slug
from .job_events import JobFailure

FORMAT_CONTRACT_VERSION = 1
MAX_DETECTION_BYTES = 4096
MAX_CLAIM_EVIDENCE = 16
MAX_RECOGNITION_ITEMS = 32
RESERVED_SAFE_POINT_PREFIXES = ("format_", "artifact_")

_EVIDENCE = re.compile(r"[a-z][a-z0-9_]{0,31}:[a-z0-9_][a-z0-9_./-]{0,127}")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,16}")
_MEDIA_TYPE = re.compile(r"[a-z]+/[a-z0-9][a-z0-9.+-]{0,100}")
_FORMAT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


class FormatError(ValueError):
    """Its message is always a bounded code, never document text or a path."""


class FormatCancelledError(FormatError):
    """Raised at a safe point. An adapter lets it pass, so no partial work can be returned."""


class FormatOperationError(FormatError):
    """How an adapter reports an expected failure, already sanitized."""

    def __init__(self, failure: JobFailure) -> None:
        if type(failure) is not JobFailure:
            raise FormatError("invalid_failure")
        super().__init__(failure.code)
        self.failure = failure


class FormatOperation(StrEnum):
    INSPECT = "inspect"
    EXTRACT = "extract"
    EXPORT_VISIBLE_CONTENT = "export_visible_content"
    PREVIEW = "preview"
    RECONSTRUCT = "reconstruct"
    VALIDATE = "validate"


MATERIALIZING_OPERATIONS = frozenset({FormatOperation.PREVIEW, FormatOperation.RECONSTRUCT})


class OperationSupport(StrEnum):
    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


RUNNABLE_SUPPORT = frozenset({OperationSupport.SUPPORTED, OperationSupport.EXPERIMENTAL})


class FidelityAspect(StrEnum):
    STRUCTURE = "structure"
    INLINE_MARKUP = "inline_markup"
    STYLES = "styles"
    IMAGES = "images"
    LINKS = "links"
    NAVIGATION = "navigation"
    METADATA = "metadata"
    INTERNAL_NAMES = "internal_names"


class FidelityLevel(StrEnum):
    PRESERVED = "preserved"
    BEST_EFFORT = "best_effort"
    LOST = "lost"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


_FIDELITY_RANK = {FidelityLevel.LOST: 0, FidelityLevel.BEST_EFFORT: 1, FidelityLevel.PRESERVED: 2}


class ContentScope(StrEnum):
    BODY = "body"
    METADATA = "metadata"
    NAVIGATION = "navigation"
    IMAGE_ALT_TEXT = "image_alt_text"


class FormatMaturity(StrEnum):
    """Informative only: negotiation never reads it, so it cannot overstate a capability."""

    EXPERIMENTAL = "experimental"
    STABLE = "stable"


def _valid_evidence(evidence: object) -> bool:
    return (type(evidence) is tuple and len(evidence) <= MAX_CLAIM_EVIDENCE
            and all(isinstance(item, str) and _EVIDENCE.fullmatch(item) for item in evidence))


@dataclass(frozen=True)
class OperationClaim:
    operation: FormatOperation
    support: OperationSupport
    evidence: tuple[str, ...] = ()
    safe_points: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.operation) is not FormatOperation or type(self.support) is not OperationSupport
                or not _valid_evidence(self.evidence) or type(self.safe_points) is not tuple
                or not all(is_slug(point) for point in self.safe_points)
                or len(set(self.safe_points)) != len(self.safe_points)):
            raise FormatError("invalid_operation_claim")
        if any(point.startswith(RESERVED_SAFE_POINT_PREFIXES) for point in self.safe_points):
            raise FormatError("reserved_safe_point")
        if self.support is OperationSupport.SUPPORTED and not self.evidence:
            raise FormatError("contradictory_claim")
        if self.support not in RUNNABLE_SUPPORT and (self.evidence or self.safe_points):
            raise FormatError("contradictory_claim")


@dataclass(frozen=True)
class FidelityClaim:
    aspect: FidelityAspect
    level: FidelityLevel
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.aspect) is not FidelityAspect or type(self.level) is not FidelityLevel
                or not _valid_evidence(self.evidence)):
            raise FormatError("invalid_fidelity_claim")
        if bool(self.evidence) != (self.level in _FIDELITY_RANK):
            raise FormatError("contradictory_claim")


@dataclass(frozen=True)
class FormatSignature:
    """Bytes that must all appear at their offsets, inside the bounded detection window."""

    parts: tuple[tuple[int, bytes], ...]

    def __post_init__(self) -> None:
        if type(self.parts) is not tuple or not self.parts:
            raise FormatError("invalid_signature")
        for part in self.parts:
            if (type(part) is not tuple or len(part) != 2 or type(part[0]) is not int or part[0] < 0
                    or type(part[1]) is not bytes or not part[1]
                    or part[0] + len(part[1]) > MAX_DETECTION_BYTES):
                raise FormatError("invalid_signature")

    def matches(self, head: bytes) -> bool:
        return all(head[offset:offset + len(expected)] == expected for offset, expected in self.parts)


@dataclass(frozen=True)
class FormatLimits:
    """``None`` means not known, never unlimited."""

    max_source_bytes: int | None = None
    max_documents: int | None = None

    def __post_init__(self) -> None:
        for value in (self.max_source_bytes, self.max_documents):
            if value is not None and (type(value) is not int or value < 1):
                raise FormatError("invalid_limit")


@dataclass(frozen=True, repr=False)
class VisibleContentDocument:
    """Ordered, already-selected visible text from one source document.

    The adapter owns the document key and output stem. The writer sees text
    fragments only; neither value is allowed into the default representation.
    """

    logical_key: str
    output_stem: str
    fragments: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.logical_key, str) or not self.logical_key
                or not isinstance(self.output_stem, str) or not self.output_stem
                or type(self.fragments) is not tuple or not all(isinstance(item, str) for item in self.fragments)):
            raise FormatError("invalid_visible_content")

    def __repr__(self) -> str:
        return f"VisibleContentDocument(fragments={len(self.fragments)})"


def _valid_locators(locators: object) -> bool:
    """One format can place content in more than one way, such as a body and a package file."""
    return (type(locators) is tuple and 1 <= len(locators) <= MAX_RECOGNITION_ITEMS and len(set(locators)) == len(locators)
            and all(type(item) is tuple and len(item) == 2 and is_slug(item[0]) and type(item[1]) is int and item[1] >= 1
                    for item in locators))


def _strings(values: object, pattern: re.Pattern) -> bool:
    return (type(values) is tuple and len(values) <= MAX_RECOGNITION_ITEMS and len(set(values)) == len(values)
            and all(isinstance(value, str) and pattern.fullmatch(value) for value in values))


@dataclass(frozen=True, repr=False)
class FormatDescriptor:
    adapter: AdapterIdentity
    locators: tuple[tuple[str, int], ...]
    operations: tuple[OperationClaim, ...]
    fidelity: tuple[FidelityClaim, ...]
    media_types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    signatures: tuple[FormatSignature, ...] = ()
    format_versions: tuple[str, ...] = ()
    content_scopes: frozenset[ContentScope] = frozenset()
    limits: FormatLimits = FormatLimits()
    maturity: FormatMaturity = FormatMaturity.EXPERIMENTAL
    contract_version: int = FORMAT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (type(self.adapter) is not AdapterIdentity or not _valid_locators(self.locators)
                or type(self.contract_version) is not int or self.contract_version < 1
                or not _strings(self.media_types, _MEDIA_TYPE) or not _strings(self.extensions, _EXTENSION)
                or not _strings(self.format_versions, _FORMAT_VERSION)
                or type(self.signatures) is not tuple or len(self.signatures) > MAX_RECOGNITION_ITEMS
                or any(type(signature) is not FormatSignature for signature in self.signatures)
                or type(self.limits) is not FormatLimits or type(self.maturity) is not FormatMaturity
                or type(self.content_scopes) is not frozenset
                or any(type(scope) is not ContentScope for scope in self.content_scopes)):
            raise FormatError("invalid_descriptor")
        if not (self.media_types or self.extensions or self.signatures):
            raise FormatError("invalid_descriptor")
        self._require_every_member(self.operations, OperationClaim, "operation", FormatOperation)
        self._require_every_member(self.fidelity, FidelityClaim, "aspect", FidelityAspect)
        materializes = any(self.runs(operation) for operation in MATERIALIZING_OPERATIONS)
        if not materializes and any(claim.level in _FIDELITY_RANK for claim in self.fidelity):
            raise FormatError("contradictory_claim")  # Nothing is written, so nothing can be preserved.
        if self.runs(FormatOperation.EXTRACT) != bool(self.content_scopes):
            raise FormatError("contradictory_claim")

    @staticmethod
    def _require_every_member(claims: object, kind: type, field: str, members: type[StrEnum]) -> None:
        if type(claims) is not tuple or any(type(claim) is not kind for claim in claims):
            raise FormatError("invalid_descriptor")
        stated = [getattr(claim, field) for claim in claims]
        if len(stated) != len(members) or set(stated) != set(members):
            raise FormatError("incomplete_descriptor")  # Absence must not read as support.

    def __repr__(self) -> str:
        return f"FormatDescriptor(adapter={self.adapter!r}, contract_version={self.contract_version})"

    @property
    def adapter_id(self) -> str:
        return self.adapter.namespace

    def claim(self, operation: FormatOperation) -> OperationClaim:
        return next(claim for claim in self.operations if claim.operation is operation)

    def runs(self, operation: FormatOperation) -> bool:
        return self.claim(operation).support in RUNNABLE_SUPPORT

    def fidelity_of(self, aspect: FidelityAspect) -> FidelityLevel:
        return next(claim.level for claim in self.fidelity if claim.aspect is aspect)

    def recognizes(self, extension: str | None, media_type: str | None) -> bool:
        return extension in self.extensions or media_type in self.media_types


@dataclass(frozen=True)
class FidelityRequirement:
    aspect: FidelityAspect
    minimum: FidelityLevel

    def __post_init__(self) -> None:
        if (type(self.aspect) is not FidelityAspect or type(self.minimum) is not FidelityLevel
                or self.minimum not in (FidelityLevel.PRESERVED, FidelityLevel.BEST_EFFORT)):
            raise FormatError("invalid_requirement")


@dataclass(frozen=True)
class FormatRequirements:
    operations: tuple[FormatOperation, ...] = ()
    fidelity: tuple[FidelityRequirement, ...] = ()
    scopes: frozenset[ContentScope] = frozenset()
    allow_experimental: bool = False

    def __post_init__(self) -> None:
        if (type(self.operations) is not tuple or any(type(item) is not FormatOperation for item in self.operations)
                or type(self.fidelity) is not tuple or any(type(item) is not FidelityRequirement for item in self.fidelity)
                or type(self.scopes) is not frozenset or any(type(item) is not ContentScope for item in self.scopes)
                or type(self.allow_experimental) is not bool):
            raise FormatError("invalid_requirement")


@dataclass(frozen=True)
class FormatNegotiation:
    accepted: bool
    code: str | None = None


def negotiate_format(descriptor: FormatDescriptor, requirements: FormatRequirements) -> FormatNegotiation:
    """Pure, and unlike provider negotiation it has no "ask later": fidelity has no runtime probe."""
    for operation in requirements.operations:
        support = descriptor.claim(operation).support
        if support is OperationSupport.UNSUPPORTED:
            return FormatNegotiation(False, "unsupported_operation")
        if support is OperationSupport.UNKNOWN:
            return FormatNegotiation(False, "unknown_operation")
        if support is OperationSupport.EXPERIMENTAL and not requirements.allow_experimental:
            return FormatNegotiation(False, "experimental_not_allowed")
    if not requirements.scopes <= descriptor.content_scopes:
        return FormatNegotiation(False, "unsupported_scope")
    for requirement in requirements.fidelity:
        level = descriptor.fidelity_of(requirement.aspect)
        if level is FidelityLevel.UNKNOWN:
            return FormatNegotiation(False, "unknown_fidelity")
        if level is not FidelityLevel.NOT_APPLICABLE and _FIDELITY_RANK[level] < _FIDELITY_RANK[requirement.minimum]:
            return FormatNegotiation(False, "insufficient_fidelity")
    return FormatNegotiation(True)


@dataclass(frozen=True, repr=False)
class DetectionEvidence:
    head: bytes
    extension: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if (type(self.head) is not bytes or len(self.head) > MAX_DETECTION_BYTES
                or (self.extension is not None and not (isinstance(self.extension, str) and _EXTENSION.fullmatch(self.extension)))
                or (self.media_type is not None and not (isinstance(self.media_type, str) and _MEDIA_TYPE.fullmatch(self.media_type)))):
            raise FormatError("invalid_detection_evidence")

    def __repr__(self) -> str:
        return f"DetectionEvidence(bytes={len(self.head)})"


class DetectionOutcome(StrEnum):
    NO_MATCH = "no_match"
    MATCH = "match"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class DetectionResult:
    outcome: DetectionOutcome
    reason: str
    candidates: tuple[str, ...] = ()

    @property
    def adapter_id(self) -> str | None:
        return self.candidates[0] if self.outcome is DetectionOutcome.MATCH else None


@dataclass(frozen=True, repr=False)
class ExtractionRequest:
    """``selection`` is what the application already resolved; an adapter never chooses."""

    project_id: str
    scopes: frozenset[ContentScope] = frozenset({ContentScope.BODY})
    selection: tuple[str, ...] | None = None
    source_locale: SourceLocale = UNKNOWN_LOCALE
    allow_experimental: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.project_id, str) or type(self.scopes) is not frozenset or not self.scopes
                or any(type(scope) is not ContentScope for scope in self.scopes)
                or type(self.source_locale) is not SourceLocale or type(self.allow_experimental) is not bool):
            raise FormatError("invalid_extraction_request")
        if self.selection is not None and (
                type(self.selection) is not tuple or not self.selection
                or not all(isinstance(key, str) and key for key in self.selection)
                or len(set(self.selection)) != len(self.selection)):
            raise FormatError("invalid_selection")

    def __repr__(self) -> str:
        return f"ExtractionRequest(scopes={sorted(self.scopes)!r})"


@dataclass(frozen=True, repr=False)
class UnitTranslation:
    """A unit ID holds its revision, so a translation of another revision is an unknown unit."""

    unit_id: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id or not isinstance(self.text, str) or not self.text:
            raise FormatError("invalid_translation")
        try:
            self.text.encode("utf-8")
        except UnicodeError:
            raise FormatError("invalid_translation") from None

    def __repr__(self) -> str:
        return f"UnitTranslation(unit_id={self.unit_id!r})"


@dataclass(frozen=True, repr=False)
class _MaterializationRequest:
    documents: tuple[ContentDocument, ...]
    translations: tuple[UnitTranslation, ...]
    requirements: FormatRequirements = FormatRequirements()

    def __post_init__(self) -> None:
        if (type(self.documents) is not tuple or not self.documents
                or any(type(document) is not ContentDocument for document in self.documents)
                or len({document.document_id for document in self.documents}) != len(self.documents)
                or type(self.requirements) is not FormatRequirements):
            raise FormatError("invalid_materialization_request")
        if type(self.translations) is not tuple or any(type(item) is not UnitTranslation for item in self.translations):
            raise FormatError("invalid_materialization_request")
        if len({item.unit_id for item in self.translations}) != len(self.translations):
            raise FormatError("duplicate_translation")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(documents={len(self.documents)}, translations={len(self.translations)})"


class PreviewRequest(_MaterializationRequest):
    """Renders the results it is given, which may be partial. It never asks for a translation."""


class ReconstructionRequest(_MaterializationRequest):
    """Needs a result for every unit of every document."""


class AliasVerdict(StrEnum):
    DISTINCT = "distinct"
    SAME = "same"
    UNKNOWN = "unknown"


@dataclass(frozen=True, repr=False)
class InventoryDocument:
    """What an application needs to resolve a selection into logical keys before extraction."""

    logical_key: str
    title: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.logical_key, str) or not self.logical_key or (self.title is not None and not isinstance(self.title, str)):
            raise FormatError("invalid_inventory")

    def __repr__(self) -> str:
        return "InventoryDocument()"


@dataclass(frozen=True, repr=False)
class FormatInventory:
    document_count: int
    resource_count: int
    format_version: str | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    language: str | None = None
    documents: tuple[InventoryDocument, ...] = ()
    source_document_count: int | None = None

    def __post_init__(self) -> None:
        if (type(self.documents) is not tuple or any(type(item) is not InventoryDocument for item in self.documents)
                or len({item.logical_key for item in self.documents}) != len(self.documents)
                or (self.documents and len(self.documents) != self.document_count)):
            raise FormatError("invalid_inventory")
        if (type(self.document_count) is not int or self.document_count < 0
                or type(self.resource_count) is not int or self.resource_count < 0
                or (self.title is not None and not isinstance(self.title, str))
                or (self.language is not None and not isinstance(self.language, str))
                or type(self.authors) is not tuple or not all(isinstance(author, str) for author in self.authors)
                or (self.source_document_count is not None
                    and (type(self.source_document_count) is not int or self.source_document_count < 0))):
            raise FormatError("invalid_inventory")

    def __repr__(self) -> str:
        return (f"FormatInventory(document_count={self.document_count}, resource_count={self.resource_count}, "
                f"source_document_count={self.source_document_count})")


class FindingSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationFinding:
    severity: FindingSeverity
    code: str
    count: int = 1

    def __post_init__(self) -> None:
        if (type(self.severity) is not FindingSeverity or not is_slug(self.code)
                or type(self.count) is not int or self.count < 1):
            raise FormatError("invalid_finding")


def _exactly_one(value_present: bool, failure: object) -> None:
    if failure is not None and type(failure) is not JobFailure:
        raise FormatError("invalid_result")
    if value_present == (failure is not None):
        raise FormatError("invalid_result")


@dataclass(frozen=True, repr=False)
class InspectionResult:
    inventory: FormatInventory | None = None
    failure: JobFailure | None = None
    source_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _exactly_one(self.inventory is not None, self.failure)
        if ((self.inventory is not None and not valid_source_fingerprint(self.source_fingerprint))
                or (self.failure is not None and self.source_fingerprint is not None)):
            raise FormatError("invalid_result")

    def __repr__(self) -> str:
        return "InspectionResult(success)" if self.inventory is not None else f"InspectionResult(failure={self.failure!r})"


@dataclass(frozen=True, repr=False)
class VisibleContentExtractionResult:
    documents: tuple[VisibleContentDocument, ...] | None = None
    source_fingerprint: str | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        _exactly_one(self.documents is not None, self.failure)
        if self.documents is not None:
            if (type(self.documents) is not tuple or any(type(item) is not VisibleContentDocument for item in self.documents)
                    or not valid_source_fingerprint(self.source_fingerprint)):
                raise FormatError("invalid_result")
        elif self.source_fingerprint is not None:
            raise FormatError("invalid_result")

    def __repr__(self) -> str:
        if self.failure is not None:
            return f"VisibleContentExtractionResult(failure={self.failure!r})"
        return f"VisibleContentExtractionResult(documents={len(self.documents or ())})"


@dataclass(frozen=True)
class ExtractionResult:
    documents: tuple[ContentDocument, ...] | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        _exactly_one(self.documents is not None, self.failure)


@dataclass(frozen=True)
class MaterializationResult:
    artifact: Artifact | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        _exactly_one(self.artifact is not None, self.failure)


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[ValidationFinding, ...] | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        _exactly_one(self.findings is not None, self.failure)

    @property
    def ok(self) -> bool:
        return self.findings is not None and all(item.severity is not FindingSeverity.ERROR for item in self.findings)


def valid_source_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _FINGERPRINT.fullmatch(value) is not None
