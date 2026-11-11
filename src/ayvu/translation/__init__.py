"""Translation (D1 of docs/architecture/decisions/package-structure.md).

The flat ``translation_*`` modules move here in REF-11; the errors arrived first, in REF-04."""

from __future__ import annotations

from .errors import (
    BatchOutputError, BookRefused, PreflightError, PreflightPolicyError, ProjectTranslationUnavailable,
    ResumeStateError, TranslationDryRunCancelled,
)

__all__ = [
    "BatchOutputError",
    "BookRefused",
    "PreflightError",
    "PreflightPolicyError",
    "ProjectTranslationUnavailable",
    "ResumeStateError",
    "TranslationDryRunCancelled",
]
