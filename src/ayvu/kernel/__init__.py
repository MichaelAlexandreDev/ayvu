"""Shared kernel (D4 of docs/architecture/decisions/package-structure.md).

Only what three or more modules use lives here, and the kernel imports no other Ayvu module.
The SQLite migration runner is infrastructure, so it has its own API in ``ayvu.kernel.sqlite``."""

from __future__ import annotations

from .errors import (
    JobError, JobValidationError, JobEventError,
)
from .ids import (
    validate_job_identity, is_slug,
)
from .clock import (
    validate_utc,
)
from .failures import (
    MAX_DIAGNOSTICS, MAX_DIAGNOSTICS_BYTES, MAX_METRIC, DIAGNOSTIC_KEYS, JobFailureFamily, valid_metric,
    job_diagnostics, validate_diagnostics, JobWarning, JobFailure, JobOperationError, normalize_failure,
)
from .cancellation import (
    JobCancellationError, SafePointObserver, CancellationToken,
)
from .values import (
    UserMode, LanguagePairError, ChapterSelectionError, TranslationMemoryError, LanguagePair, OutputPlan,
    automatic_output_candidate, default_translated_books_dir, default_preview_books_dir,
    ChapterSelectionItem, ChapterSelection, TranslationMemoryOptions, PerformanceProfile,
    PERFORMANCE_PROFILE_LOW, PERFORMANCE_PROFILE_STANDARD, PERFORMANCE_PROFILE_HIGH,
    PERFORMANCE_PROFILE_CUSTOM, DEFAULT_PERFORMANCE_PROFILE, PERFORMANCE_PROFILES, TranslationOptions,
)

__all__ = [
    "CancellationToken",
    "ChapterSelection",
    "ChapterSelectionError",
    "ChapterSelectionItem",
    "DEFAULT_PERFORMANCE_PROFILE",
    "DIAGNOSTIC_KEYS",
    "JobCancellationError",
    "JobError",
    "JobEventError",
    "JobFailure",
    "JobFailureFamily",
    "JobOperationError",
    "JobValidationError",
    "JobWarning",
    "LanguagePair",
    "LanguagePairError",
    "MAX_DIAGNOSTICS",
    "MAX_DIAGNOSTICS_BYTES",
    "MAX_METRIC",
    "OutputPlan",
    "PERFORMANCE_PROFILES",
    "PERFORMANCE_PROFILE_CUSTOM",
    "PERFORMANCE_PROFILE_HIGH",
    "PERFORMANCE_PROFILE_LOW",
    "PERFORMANCE_PROFILE_STANDARD",
    "PerformanceProfile",
    "SafePointObserver",
    "TranslationMemoryError",
    "TranslationMemoryOptions",
    "TranslationOptions",
    "UserMode",
    "automatic_output_candidate",
    "default_preview_books_dir",
    "default_translated_books_dir",
    "is_slug",
    "job_diagnostics",
    "normalize_failure",
    "valid_metric",
    "validate_diagnostics",
    "validate_job_identity",
    "validate_utc",
]
