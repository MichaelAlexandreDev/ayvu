"""Governed TM values. Approval is review evidence, never dataset consent.

Linguistic values and all their derived keys are sensitive. No value object's
representation includes caller data. Identity comes from the content model,
not from the operational cache or from a match on source text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re
from types import MappingProxyType
import unicodedata

from .content import ContentDocument
from .domain import LanguagePair
from .job import JobError, validate_job_identity
from .provenance import ConfigurationRef

MAX_VALUE_BYTES = 64 * 1024
NORMALIZATION_VERSION = "nfc-v1-ucd" + unicodedata.unidata_version
SEARCH_VERSION = "nfc-casefold-whitespace-v1-ucd" + unicodedata.unidata_version


class MemoryError(ValueError):
    """Only fixed codes may cross the application boundary."""


class MemoryValidationError(MemoryError):
    pass


class MemoryNotFoundError(MemoryError):
    pass


class MemoryConflictError(MemoryError):
    pass


class MemoryTransitionError(MemoryError):
    pass


class MemoryScopeError(MemoryError):
    pass


class MemoryValueError(MemoryError):
    pass


class MemoryStorageError(MemoryError):
    pass


class MemoryProvenanceError(MemoryError):
    pass


def identity(value: str) -> None:
    try:
        validate_job_identity(value)
    except JobError:
        raise MemoryValidationError("invalid_identity") from None


def bounded_value(value: str) -> None:
    if not isinstance(value, str):
        raise MemoryValueError("invalid_linguistic_value")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise MemoryValueError("invalid_linguistic_value") from None
    if size > MAX_VALUE_BYTES:
        raise MemoryValueError("linguistic_value_too_large")


class MemoryState(StrEnum):
    MACHINE_CANDIDATE = "machine_candidate"
    HUMAN_EDITED = "human_edited"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


TRANSITIONS = MappingProxyType({
    MemoryState.MACHINE_CANDIDATE: frozenset({MemoryState.HUMAN_EDITED, MemoryState.APPROVED,
                                            MemoryState.REJECTED, MemoryState.DEPRECATED}),
    MemoryState.HUMAN_EDITED: frozenset({MemoryState.APPROVED, MemoryState.REJECTED, MemoryState.DEPRECATED}),
    MemoryState.APPROVED: frozenset({MemoryState.DEPRECATED, MemoryState.SUPERSEDED}),
    MemoryState.REJECTED: frozenset(),
    MemoryState.DEPRECATED: frozenset(),
    MemoryState.SUPERSEDED: frozenset(),
})


def require_transition(previous: MemoryState, following: MemoryState) -> None:
    if (type(previous) is not MemoryState or type(following) is not MemoryState
            or following not in TRANSITIONS[previous]):
        raise MemoryTransitionError("invalid_transition")


class ScopeKind(StrEnum):
    PROJECT = "project"
    WORK = "work"
    USER = "user"
    IMPORT = "import"


@dataclass(frozen=True, repr=False)
class MemoryScope:
    kind: ScopeKind
    owner_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not ScopeKind:
            raise MemoryScopeError("invalid_scope")
        identity(self.owner_id)


class ValueKind(StrEnum):
    PROVIDER_RESPONSE = "provider_response"
    RESTORED_VISIBLE = "restored_visible"
    GLOSSARY_DERIVED = "glossary_derived"
    LINGUISTIC_FINAL = "linguistic_final"


class PipelineStage(StrEnum):
    PROVIDER = "provider"
    PROTECTED_RESTORED = "protected_restored"
    TERMINOLOGY_APPLIED = "terminology_applied"
    LINGUISTIC_FINAL = "linguistic_final"


VALUE_STAGES = MappingProxyType(dict(zip(ValueKind, PipelineStage)))


@dataclass(frozen=True, repr=False)
class PipelinePin:
    pipeline: ConfigurationRef
    profile: ConfigurationRef
    glossary: ConfigurationRef

    def __post_init__(self) -> None:
        for kind, ref in zip(("pipeline", "profile", "glossary"), self.references):
            if type(ref) is not ConfigurationRef or ref.kind != kind:
                raise MemoryValueError("invalid_pipeline_pin")

    @property
    def references(self) -> tuple[ConfigurationRef, ...]:
        return self.pipeline, self.profile, self.glossary


@dataclass(frozen=True, repr=False)
class TargetValue:
    text: str
    kind: ValueKind
    stage: PipelineStage
    pin: PipelinePin

    def __post_init__(self) -> None:
        bounded_value(self.text)
        if (type(self.kind) is not ValueKind or type(self.stage) is not PipelineStage
                or VALUE_STAGES[self.kind] is not self.stage or type(self.pin) is not PipelinePin):
            raise MemoryValueError("incompatible_value_stage")

    def require_approval_eligible(self) -> None:
        if self.kind is not ValueKind.LINGUISTIC_FINAL or self.stage is not PipelineStage.LINGUISTIC_FINAL:
            raise MemoryValueError("approval_requires_linguistic_final")


def search_key(canonical: str) -> str:
    return unicodedata.normalize("NFC", " ".join(canonical.casefold().split()))


@dataclass(frozen=True, repr=False)
class MemorySource:
    project_id: str
    document_id: str
    document_revision: str
    unit_id: str
    content_id: str
    canonical_text: str
    search_key: str
    normalization_version: str = NORMALIZATION_VERSION
    search_version: str = SEARCH_VERSION

    def __post_init__(self) -> None:
        identity(self.project_id)
        for prefix, value in (("doc", self.document_id), ("rev", self.document_revision),
                              ("unit", self.unit_id), ("scd", self.content_id)):
            if not isinstance(value, str) or not re.fullmatch(prefix + r":v1:[0-9a-f]{64}", value):
                raise MemoryValidationError("invalid_content_identity")
        bounded_value(self.canonical_text)
        if (self.normalization_version != NORMALIZATION_VERSION or self.search_version != SEARCH_VERSION
                or unicodedata.normalize("NFC", self.canonical_text) != self.canonical_text
                or self.search_key != search_key(self.canonical_text)):
            raise MemoryValidationError("invalid_search_policy")

    @classmethod
    def from_document(cls, document: ContentDocument, unit_id: str, *, visible_text: str | None = None) -> MemorySource:
        if type(document) is not ContentDocument:
            raise MemoryValidationError("invalid_content_document")
        unit = next((item for item in document.units if item.unit_id == unit_id), None)
        if unit is None:
            raise MemoryNotFoundError("unit_not_found")
        # Only the format adapter knows visible semantics such as line breaks
        # and hidden inline content. A structural span cannot supply that value.
        has_markup = any(part.kind == "markup" for part in unit.content.protected_parts)
        if has_markup and visible_text is None:
            raise MemoryValidationError("visible_source_required")
        if not has_markup and visible_text is not None and visible_text != unit.source_text:
            raise MemoryValidationError("visible_source_mismatch")
        visible = unit.source_text if visible_text is None else visible_text
        # Reject before normalization too: normalization cannot hide an oversized input.
        bounded_value(visible)
        canonical = unicodedata.normalize("NFC", visible)
        return cls(document.project_id, document.document_id, document.revision_id, unit.unit_id,
                   unit.source_digest, canonical, search_key(canonical))


def validate_pair(pair: LanguagePair) -> None:
    if (type(pair) is not LanguagePair or any(not isinstance(code, str)
            or not re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", code) or code in {"auto", "unknown"}
            for code in (pair.source, pair.target))):
        raise MemoryValidationError("invalid_language_pair")


@dataclass(frozen=True, repr=False)
class Candidate:
    source: MemorySource
    languages: LanguagePair
    target: TargetValue
    scope: MemoryScope
    provenance_id: str
    import_origin: str | None = None

    def __post_init__(self) -> None:
        if (type(self.source) is not MemorySource or type(self.target) is not TargetValue
                or type(self.scope) is not MemoryScope):
            raise MemoryValidationError("invalid_candidate")
        validate_pair(self.languages)
        identity(self.provenance_id)
        if self.import_origin is not None:
            identity(self.import_origin)
        if self.scope.kind is ScopeKind.IMPORT and self.import_origin is None:
            raise MemoryProvenanceError("import_origin_required")
        if self.scope.kind is ScopeKind.PROJECT and self.scope.owner_id != self.source.project_id:
            raise MemoryScopeError("scope_denied")


@dataclass(frozen=True, repr=False)
class MemoryEvent:
    version: int
    state: MemoryState
    provenance_id: str
    recorded_at: datetime


@dataclass(frozen=True, repr=False)
class MemoryRevision:
    sequence: int
    entry_id: str
    revision_id: str
    revision_number: int
    parent_revision: str | None
    value: Candidate
    event: MemoryEvent

    @property
    def state(self) -> MemoryState:
        return self.event.state


@dataclass(frozen=True, repr=False)
class MemoryPage:
    revisions: tuple[MemoryRevision, ...]
    next_cursor: int | None
