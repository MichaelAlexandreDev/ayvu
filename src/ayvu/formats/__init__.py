"""Document formats: the format contracts and the service that runs an adapter. Local files are the adapter
module formats.local; each format is a subpackage.
"""
from __future__ import annotations

from .contracts import (
    AliasVerdict, ContentScope, DetectionOutcome, DetectionResult, ExtractionRequest, FormatDescriptor,
    FormatError, FormatInventory, ReconstructionRequest, UnitTranslation, VisibleContentDocument,
    valid_source_fingerprint,
)
from .service import FormatPublication, FormatRegistry, FormatService, FormatSource
from .use_cases import (
    CONTENT_BEFORE_EXPORT, CONTENT_BEFORE_INSPECTION, CONTENT_BEFORE_PUBLICATION,
    CONTENT_BETWEEN_DOCUMENTS, CONTENT_SAFE_POINTS, ContentProgress, ContentUseCaseError,
    ExportFileOutcome, ExportFileState, ExportVisibleContent, ExportVisibleContentRequest,
    ExportVisibleContentResult, InspectContent, InspectContentRequest, InspectContentResult,
    OutputDirectoryRef, PlannedOutputFile, ProgressSink, PublishedFileRef, SourceRef, SourceRevision,
    VisibleContentWriter, WriterAuthorization, WriterPlan, WriterPublication,
)

__all__ = [
    "AliasVerdict", "ContentScope", "DetectionOutcome", "DetectionResult", "ExtractionRequest",
    "FormatDescriptor", "FormatError", "FormatInventory", "FormatPublication", "FormatRegistry",
    "FormatService", "FormatSource", "ReconstructionRequest", "UnitTranslation", "VisibleContentDocument",
    "valid_source_fingerprint",
    "CONTENT_BEFORE_EXPORT", "CONTENT_BEFORE_INSPECTION", "CONTENT_BEFORE_PUBLICATION",
    "CONTENT_BETWEEN_DOCUMENTS", "CONTENT_SAFE_POINTS", "ContentProgress", "ContentUseCaseError",
    "ExportFileOutcome", "ExportFileState", "ExportVisibleContent", "ExportVisibleContentRequest",
    "ExportVisibleContentResult", "InspectContent", "InspectContentRequest", "InspectContentResult",
    "OutputDirectoryRef", "PlannedOutputFile", "ProgressSink", "PublishedFileRef", "SourceRef",
    "SourceRevision", "VisibleContentWriter", "WriterAuthorization", "WriterPlan", "WriterPublication",
]
