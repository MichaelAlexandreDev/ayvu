"""Document formats: the format contracts and the service that runs an adapter. Local files are the adapter
module formats.local; each format is a subpackage.
"""
from __future__ import annotations

from .book_facts import BookFacts, BookPackage
from .contracts import (
    AliasVerdict, ContentScope, DetectionOutcome, DetectionResult, ExtractionRequest, FormatDescriptor,
    FormatError, FormatInventory, ReconstructionRequest, UnitTranslation, VisibleContentDocument,
    PreviewRequest, FormatOperation, FormatOperationError, FormatCancelledError,
    valid_source_fingerprint,
)
from .service import FormatContext, FormatPublication, FormatRegistry, FormatService, FormatSource
from .use_cases import (
    CONTENT_BEFORE_EXPORT, CONTENT_BEFORE_INSPECTION, CONTENT_BEFORE_PUBLICATION,
    CONTENT_BETWEEN_DOCUMENTS, CONTENT_SAFE_POINTS, ContentProgress, ContentUseCaseError,
    ExportFileOutcome, ExportFileState, ExportVisibleContent, ExportVisibleContentRequest,
    ExportVisibleContentResult, InspectContent, InspectContentRequest, InspectContentResult,
    OutputDirectoryRef, PlannedOutputFile, ProgressSink, PublishedFileRef, SourceRef, SourceRevision,
    VisibleContentWriter, WriterAuthorization, WriterPlan, WriterPublication,
)

__all__ = [
    "BookFacts", "BookPackage", "AliasVerdict", "ContentScope", "DetectionOutcome", "DetectionResult", "ExtractionRequest",
    "FormatDescriptor", "FormatError", "FormatInventory", "FormatPublication", "FormatRegistry",
    "FormatService", "FormatSource", "ReconstructionRequest", "UnitTranslation", "VisibleContentDocument",
    "valid_source_fingerprint",
    "PreviewRequest", "FormatOperation", "FormatOperationError", "FormatCancelledError", "FormatContext",
    "CONTENT_BEFORE_EXPORT", "CONTENT_BEFORE_INSPECTION", "CONTENT_BEFORE_PUBLICATION",
    "CONTENT_BETWEEN_DOCUMENTS", "CONTENT_SAFE_POINTS", "ContentProgress", "ContentUseCaseError",
    "ExportFileOutcome", "ExportFileState", "ExportVisibleContent", "ExportVisibleContentRequest",
    "ExportVisibleContentResult", "InspectContent", "InspectContentRequest", "InspectContentResult",
    "OutputDirectoryRef", "PlannedOutputFile", "ProgressSink", "PublishedFileRef", "SourceRef",
    "SourceRevision", "VisibleContentWriter", "WriterAuthorization", "WriterPlan", "WriterPublication",
]
