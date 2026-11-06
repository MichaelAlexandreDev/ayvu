"""CLI workflow ports and content-free failures. No concrete adapter or UI imports."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .domain import OutputPlan, TranslationOptions, TranslationMemoryOptions
from .config_values import AyvuConfig
from .preferences_service import PreferencesSnapshot, UpdatePreferencesResult
from .job_recovery import ListInterruptedJobs, InspectJobRecoverability, RequestJobResume, RecoveryEntry
from .translate_project import ContinuationTicket, TranslateProjectResult, AcceptedOutputPlan

class GlossaryUsage(Protocol):
    applied_terms: dict[str, int]
    required_terms_present: dict[str, int]
    required_terms_missing: list[str]
    forbidden_terms_found: dict[str, int]
    @property
    def total_applied(self) -> int: ...
    @property
    def total_forbidden_found(self) -> int: ...
    @property
    def has_activity(self) -> bool: ...


class TranslationReport(Protocol):
    chapters_processed: int
    texts_translated: int
    texts_from_cache: int
    texts_from_memory: int
    memory_suggestions: int
    texts_skipped: int
    texts_missing: int
    alt_texts_translated: int
    errors: list[str]
    missing_texts: list[str]
    memory_suggestion_texts: list[str]
    glossary_terms_configured: int
    glossary_usage: GlossaryUsage
    output_path: Path | None
    output_written: bool
    review_output_path: Path | None
    input_path: Path | None
    detected_language: str | None
    target_language: str | None

class ValidationResult(Protocol):
    ok: bool
    document_count: int
    warnings: list[str]

class AcceptedItem(AcceptedOutputPlan, Protocol):
    def revalidate(self) -> None: ...
    def mark_published(self) -> None: ...
    def write_auxiliary(self, role: str, text: str) -> Path: ...

class ConfigStore(Protocol):
    def get_preferences(self) -> PreferencesSnapshot: ...
    def update_preferences(self, config: AyvuConfig, expected_revision: str) -> UpdatePreferencesResult: ...

class TranslationResumeState(Protocol):
    version: int
    status: str
    input_path: Path
    output_path: Path
    cache_path: Path
    source: str
    target: str
    translator_name: str
    url: str
    glossary_path: Path | None
    fail_fast: bool
    overwrite: bool
    timeout: float
    retries: int
    chunk_limit: int
    workers: int
    translate_metadata: bool
    translate_alt_text: bool
    chapter_selection: str | None
    created_at: str
    updated_at: str
    translation_memory_enabled: bool
    translation_memory_apply_threshold: float | None
    translation_memory_suggest_threshold: float | None
    translation_memory_max_candidates: int | None
    requests_per_second: float | None
    retry_backoff: float
    retry_backoff_max: float
    total_chapters: int | None
    current_chapter: str | None
    completed_chapters: tuple[str, ...]
    failed_chapters: tuple[str, ...]
    failed_segment_count: int
    profile_name: str | None
    def translation_memory_options(self) -> TranslationMemoryOptions | None: ...

class InvalidResumeState(Protocol):
    path: Path
    message: str

class JobRecovery(Protocol):
    list_interrupted: ListInterruptedJobs
    inspect: InspectJobRecoverability
    request_resume: RequestJobResume

class InterruptedTranslations(Protocol):
    running: tuple[TranslationResumeState, ...]
    invalid: tuple[InvalidResumeState, ...]
    has_findings: bool
    def entry_of(self, state: TranslationResumeState) -> RecoveryEntry: ...

class SelectedChapter(Protocol):
    archive_path: str
    index: int
    name: str
    title: str | None

class CacheSummaryRow(Protocol):
    source_lang: str
    target_lang: str
    count: int
    first_created_at: str
    last_created_at: str

class CacheImportReport(Protocol):
    imported: int
    skipped: int
    replaced: int
    total_read: int


class PreflightError(RuntimeError):
    def __init__(self, summary: str, next_step: str, detail: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.next_step = next_step
        self.detail = detail


class PreflightPolicyError(PreflightError):
    """A policy refusal remains distinguishable without parsing presentation text."""

    def __init__(self, code: str, next_step: str, detail: str) -> None:
        super().__init__("O acesso HTTP ao tradutor foi bloqueado.", next_step, detail)
        self.code = code


class BookRefused(RuntimeError):
    """The book is refused before anything starts. The message is the code, never a name from the book."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TranslationDryRunCancelled(KeyboardInterrupt):
    """The no-project path observed a request at a callback safe point."""


class ProjectTranslationUnavailable(RuntimeError):
    """The project's storage could not be opened. Nothing was started, and the message is a bounded code."""


class ProviderSetupError(ValueError):
    """A bounded error: the settings could not build a provider."""


class ResumeStateError(ValueError):
    pass


class BatchOutputError(ValueError):
    """A stable code, never a path or a system exception."""


@dataclass(frozen=True, repr=False)
class _BookHooks:
    """What one book reports to the terminal and to the resume file while it runs."""

    on_chapter_start: Callable[[int, int, str], None]
    on_chapter_done: Callable[[int, int, str, object], None]
    on_text_processed: Callable[[str], None]
    on_validation: Callable[[int, int, str], None]


@dataclass(frozen=True, repr=False)
class _BookRequest:
    epub_path: Path
    output_plan: OutputPlan
    replace_existing: bool
    options: TranslationOptions
    cache_path: Path
    prepare: Callable[[], object]
    project_id: str | None = None
    source_sha256: str | None = None
    processing_dir: Path | None = None
    continuation: ContinuationTicket | None = None
    operation_id: str | None = None
    batch_plan: AcceptedItem | None = None
    cancellation: object | None = None
    on_project_result: Callable[[TranslateProjectResult], None] | None = None


@dataclass(frozen=True, repr=False)
class _BookOutcome:
    """``failure`` is a bounded code. With it there is no report, because the run never reached one."""

    report: TranslationReport | None = None
    validation: object | None = None
    failure: str | None = None
    raised: Exception | None = None
    warning: str | None = None
    job_result: TranslateProjectResult | None = None

