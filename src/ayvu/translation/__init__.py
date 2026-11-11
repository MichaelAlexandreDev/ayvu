"""Translating a book: the plan, the pipeline and its stages, the engine and document runs, the project and
batch use cases, and their errors. Preflight and the batch output policy are adapter modules.
"""
from __future__ import annotations

from .batch import (
    BatchCancellationCommand, BatchCancellationNotice, BatchCommand, BatchItem, BatchValidationError,
    ItemOutcome, ItemStatus, TranslateBatch,
)
from .document import (
    DocumentTranslator, ExecutionContext, RAW_RESULTS_ROLE, SAFE_POINTS, TranslationExecutionError,
    UnitSource,
)
from .engine import (
    ALT_TEXT_KIND, BookFacts, EngineObserver, EngineRequest, EngineResult, ExecutorPool, METADATA_KIND,
    STATUS_CACHE, STATUS_ERROR, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED, TextStats,
    TranslationEngine, TranslationResources, UnitExecution,
)
from .errors import (
    BatchOutputError, BookRefused, PreflightError, ProjectTranslationUnavailable, ResumeStateError,
    TranslationDryRunCancelled,
)
from .pipeline import LegacyTextPipelineFacade, LinguisticSource
from .plan import resolve_plan
from .project import TranslateProject
from .project_contracts import (
    AcceptedOutputPlan, ContinuationTicket, INTERRUPTED, INTERRUPTED_AFTER_PUBLICATION, OUTPUT_ROLE,
    OWN_JOB_STATES, OutputValidation, PUBLICATION_UNCOMMITTED, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION,
    RuntimeContext, TRANSLATE_COMMAND_TYPE, TranslateCommandError, TranslateObserver,
    TranslateProjectCommand, TranslateProjectPorts, TranslateProjectResult, TranslationRuntime,
    resolve_execution_plan,
)
from .resume_reuse import (
    ArtifactIntegrity, INTEGRITY_REASONS, REUSE_DIMENSIONS, RecoveryReason, ResumeStage, StageEvidence,
    StageSelection, select_resume_stage,
)

__all__ = [
    "ALT_TEXT_KIND", "AcceptedOutputPlan", "ArtifactIntegrity", "BatchCancellationCommand",
    "BatchCancellationNotice", "BatchCommand", "BatchItem", "BatchOutputError", "BatchValidationError",
    "BookFacts", "BookRefused", "ContinuationTicket", "DocumentTranslator", "EngineObserver", "EngineRequest",
    "EngineResult", "ExecutionContext", "ExecutorPool", "INTEGRITY_REASONS", "INTERRUPTED",
    "INTERRUPTED_AFTER_PUBLICATION", "ItemOutcome", "ItemStatus", "LegacyTextPipelineFacade",
    "LinguisticSource", "METADATA_KIND", "OUTPUT_ROLE", "OWN_JOB_STATES", "OutputValidation",
    "PUBLICATION_UNCOMMITTED", "PreflightError", "ProjectTranslationUnavailable", "RAW_RESULTS_ROLE",
    "RECONSTRUCT_STAGE", "RECONSTRUCT_STAGE_VERSION", "REUSE_DIMENSIONS", "RecoveryReason", "ResumeStage",
    "ResumeStateError", "RuntimeContext", "SAFE_POINTS", "STATUS_CACHE", "STATUS_ERROR", "STATUS_MEMORY",
    "STATUS_MISSING", "STATUS_TRANSLATED", "StageEvidence", "StageSelection", "TRANSLATE_COMMAND_TYPE",
    "TextStats", "TranslateBatch", "TranslateCommandError", "TranslateObserver", "TranslateProject",
    "TranslateProjectCommand", "TranslateProjectPorts", "TranslateProjectResult",
    "TranslationDryRunCancelled", "TranslationEngine", "TranslationExecutionError", "TranslationResources",
    "TranslationRuntime", "UnitExecution", "UnitSource", "resolve_execution_plan", "resolve_plan",
    "select_resume_stage",
]
