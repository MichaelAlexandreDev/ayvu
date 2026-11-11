"""The format-neutral content model and the protection of special terms and tags."""
from __future__ import annotations

from .model import (
    AdapterIdentity, Constraint, ContentDocument, ContentError, Locator, ProtectedPart, SemanticKind,
    SourceLocale, TranslationUnit, UNKNOWN_LOCALE, UnitContent, build_document, source_digest,
    validate_protected_parts,
)
from .protection import (
    INTERNAL_MARKER_PATTERN, PROTECTED_TOKEN_PATTERN, ProtectedSpan, TAG_TOKEN_PATTERN, TextParts,
    protect_special_terms, protected_content_is_intact, protected_terms_are_intact, special_term_spans,
)

__all__ = [
    "AdapterIdentity", "Constraint", "ContentDocument", "ContentError", "INTERNAL_MARKER_PATTERN", "Locator",
    "PROTECTED_TOKEN_PATTERN", "ProtectedPart", "ProtectedSpan", "SemanticKind", "SourceLocale",
    "TAG_TOKEN_PATTERN", "TextParts", "TranslationUnit", "UNKNOWN_LOCALE", "UnitContent", "build_document",
    "protect_special_terms", "protected_content_is_intact", "protected_terms_are_intact", "source_digest",
    "special_term_spans", "validate_protected_parts",
]
