"""Translating a book: the plan, the pipeline and its stages, the engine and document runs, the project and
batch use cases, and their errors. Preflight and the batch output policy are adapter modules.
"""
from __future__ import annotations

from .batch import (
    BatchCancellationCommand, BatchCancellationNotice, BatchCommand, BatchItem, BatchValidationError,
    ItemOutcome, ItemStatus, TranslateBatch,
)
from .cache_only_execution import CacheOnlyDocumentExecutor
from .document import DocumentTranslator
from .document_work import DocumentWork, DocumentWorkResult
from .worker_cache_keys import ArbitratedExactCache
from .document_contracts import ExecutionContext, SAFE_POINTS, TranslationCancelled, TranslationExecutionError, UnitSource
from .raw_results import RAW_RESULTS_ROLE
from .engine import (
    ALT_TEXT_KIND, EngineObserver, EngineRequest, EngineResult, ExecutorPool, METADATA_KIND,
    STATUS_CACHE, STATUS_ERROR, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED, TextStats,
    TranslationEngine, TranslationResources, UnitExecution,
)
from .errors import (
    BatchOutputError, BookRefused, PreflightError, PreflightPolicyError, PreviousRuntimeRetired, ProjectTranslationUnavailable, ResumeStateError,
    TranslationDryRunCancelled,
)
from .pipeline import LegacyTextPipelineFacade, LinguisticSource
from .plan import resolve_plan
from .local_cache_plan import (
    CanonicalCacheRecordsPort, LocalCachePlanIntent, LocalCachePlanResolution,
    LocalCachePlanResolver, LocalCachePlanState,
)
from .project import TranslateProject
from .project_contracts import (
    AcceptedOutputPlan, ContinuationTicket, INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, OUTPUT_ROLE,
    OWN_JOB_STATES, OutputValidation, PUBLICATION_UNCOMMITTED, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION,
    RuntimeContext, TRANSLATE_COMMAND_TYPE, TranslateCommandError, TranslateObserver,
    TranslateProjectCommand, TranslateProjectPorts, TranslateProjectResult, TranslationRuntime,
    resolve_execution_plan,
)
from .report import HtmlTranslationStats, TextTranslationResult, TranslationReport, to_html_stats, to_report
from .transient import TransientExecution, TransientTranslation
from .transient_units import TransientUnits
from .transient_units import TransientProvider
from .transient_cache import TransientCache
from .resume_reuse import (
    ArtifactIntegrity, INTEGRITY_REASONS, REUSE_DIMENSIONS, RecoveryReason, ResumeStage, StageEvidence,
    StageSelection, select_resume_stage,
)

__all__ = [
    "ALT_TEXT_KIND", "AcceptedOutputPlan", "ArtifactIntegrity", "BatchCancellationCommand",
    "BatchCancellationNotice", "BatchCommand", "BatchItem", "BatchOutputError", "BatchValidationError",
    "ArbitratedExactCache", "CacheOnlyDocumentExecutor", "BookRefused", "ContinuationTicket", "DocumentTranslator", "DocumentWork", "DocumentWorkResult", "EngineObserver", "EngineRequest",
    "EngineResult", "ExecutionContext", "ExecutorPool", "HtmlTranslationStats", "INTEGRITY_REASONS", "INTERRUPTED",
    "INTERRUPTED_AFTER_PUBLICATION", "ItemOutcome", "ItemStatus", "LegacyTextPipelineFacade",
    "LinguisticSource", "METADATA_KIND", "OUTPUT_ROLE", "OWN_JOB_STATES", "OutputValidation",
    "PUBLICATION_UNCOMMITTED", "PreflightError", "PreflightPolicyError", "PreviousRuntimeRetired", "ProjectTranslationUnavailable", "RAW_RESULTS_ROLE",
    "RECONSTRUCT_STAGE", "RECONSTRUCT_STAGE_VERSION", "REUSE_DIMENSIONS", "RecoveryReason", "ResumeStage",
    "ResumeStateError", "RuntimeContext", "SAFE_POINTS", "STATUS_CACHE", "STATUS_ERROR", "STATUS_MEMORY",
    "STATUS_MISSING", "STATUS_TRANSLATED", "StageEvidence", "StageSelection", "TRANSLATE_COMMAND_TYPE",
    "TextStats", "TextTranslationResult", "TranslateBatch", "TranslateCommandError", "TranslateObserver", "TranslateProject",
    "TranslateProjectCommand", "TranslateProjectPorts", "TranslateProjectResult",
    "TranslationCancelled", "TranslationDryRunCancelled", "TranslationEngine", "TranslationExecutionError", "TranslationResources",
    "TranslationReport", "TranslationRuntime", "UnitExecution", "UnitSource", "resolve_execution_plan", "resolve_plan",
    "CanonicalCacheRecordsPort", "LocalCachePlanIntent", "LocalCachePlanResolution",
    "LocalCachePlanResolver", "LocalCachePlanState", "select_resume_stage",
    "to_html_stats", "to_report",
    "TransientExecution", "TransientTranslation", "TransientUnits", "TransientCache", "TransientProvider",
]
