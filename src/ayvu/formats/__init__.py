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

__all__ = [
    "AliasVerdict", "ContentScope", "DetectionOutcome", "DetectionResult", "ExtractionRequest",
    "FormatDescriptor", "FormatError", "FormatInventory", "FormatPublication", "FormatRegistry",
    "FormatService", "FormatSource", "ReconstructionRequest", "UnitTranslation", "VisibleContentDocument",
    "valid_source_fingerprint",
]
