"""Whether a stage's earlier result can be reused when a translation resumes: pure rules over evidence.

The translation pipeline and recovery both decide from these rules, so they sit with translation, below
recovery. ``RecoveryReason`` is the shared vocabulary of why a job or a stage cannot simply go on.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

REUSE_DIMENSIONS = ("input", "configuration", "provider", "route", "capability", "format", "locator", "pipeline", "stage")


class RecoveryReason(StrEnum):
    RECOVERABLE_INTERRUPTION = "recoverable_interruption"
    ACTIVE_LEASE = "active_lease"
    LEASE_OWNER_GONE = "lease_owner_gone"
    EXPIRED_LEASE = "expired_lease"
    TERMINAL_JOB = "terminal_job"
    JOB_STATE_NOT_RESUMABLE = "job_state_not_resumable"
    SOURCE_CHANGED = "source_changed"
    SOURCE_MISSING = "source_missing"
    SOURCE_UNREADABLE = "source_unreadable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_STATE_FILE = "invalid_state_file"
    MISSING_CONFIGURATION = "missing_configuration"
    INCOMPATIBLE_PLAN = "incompatible_plan"
    PROJECT_UNAVAILABLE = "project_unavailable"
    INVOCATION_UNAVAILABLE = "invocation_unavailable"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_PARTIAL = "artifact_partial"
    ARTIFACT_CORRUPT = "artifact_corrupt"
    PROVENANCE_UNPROVEN = "provenance_unproven"
    DESTINATION_CONFLICT = "destination_conflict"
    POLICY_DENIED = "policy_denied"
    LEGACY_ONLY = "legacy_only"


class ResumeStage(StrEnum):
    TRANSLATE = "translate"
    RECONSTRUCT = "reconstruct"


_STAGE_ORDER = (ResumeStage.TRANSLATE, ResumeStage.RECONSTRUCT)


class ArtifactIntegrity(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    NOT_READY = "not_ready"
    # A record says it is ready, and nobody read the bytes. That is never enough to reuse it.
    UNCHECKED = "unchecked"


# ---------- Reuse: pure rules over evidence.

@dataclass(frozen=True, repr=False)
class StageEvidence:
    """What is known about one stage's artifact. A dimension nobody recorded is None, which proves nothing."""

    stage: ResumeStage
    integrity: ArtifactIntegrity
    owned: bool = True
    schema_valid: bool = True
    expected: tuple[tuple[str, str | None], ...] = ()
    produced: tuple[tuple[str, str | None], ...] = ()

    def __repr__(self) -> str:
        return f"StageEvidence(stage={self.stage.value!r}, integrity={self.integrity.value!r})"


@dataclass(frozen=True)
class StageSelection:
    """``resume_from`` is the earliest stage that must run again. None means every stage was proved."""

    resume_from: ResumeStage | None
    reusable: tuple[ResumeStage, ...] = ()
    reason: RecoveryReason | None = None
    changed: tuple[str, ...] = ()
    not_provable: tuple[str, ...] = ()


INTEGRITY_REASONS = {
    ArtifactIntegrity.MISSING: RecoveryReason.ARTIFACT_MISSING,
    ArtifactIntegrity.PARTIAL: RecoveryReason.ARTIFACT_PARTIAL,
    ArtifactIntegrity.CORRUPT: RecoveryReason.ARTIFACT_CORRUPT,
    ArtifactIntegrity.NOT_READY: RecoveryReason.ARTIFACT_MISSING,
}


def select_resume_stage(evidence: tuple[StageEvidence, ...]) -> StageSelection:
    """A stage is reusable only if its artifact is verified and owned, and every dimension is proved equal."""
    by_stage = {item.stage: item for item in evidence}
    reusable: list[ResumeStage] = []
    for stage in _STAGE_ORDER:
        item = by_stage.get(stage)
        if item is None:
            return StageSelection(stage, tuple(reusable), RecoveryReason.ARTIFACT_MISSING)
        if item.integrity is ArtifactIntegrity.UNCHECKED:
            return StageSelection(stage, tuple(reusable), RecoveryReason.PROVENANCE_UNPROVEN, not_provable=REUSE_DIMENSIONS)
        if item.integrity is not ArtifactIntegrity.VERIFIED:
            return StageSelection(stage, tuple(reusable), INTEGRITY_REASONS[item.integrity])
        if not item.owned or not item.schema_valid:
            return StageSelection(stage, tuple(reusable), RecoveryReason.ARTIFACT_CORRUPT)
        expected, produced = dict(item.expected), dict(item.produced)
        changed = tuple(name for name in REUSE_DIMENSIONS
                        if expected.get(name) is not None and produced.get(name) is not None and expected[name] != produced[name])
        if changed:
            return StageSelection(stage, tuple(reusable), RecoveryReason.INCOMPATIBLE_PLAN, changed=changed)
        unproved = tuple(name for name in REUSE_DIMENSIONS if expected.get(name) is None or produced.get(name) is None)
        if unproved:
            return StageSelection(stage, tuple(reusable), RecoveryReason.PROVENANCE_UNPROVEN, not_provable=unproved)
        reusable.append(stage)
    return StageSelection(None, tuple(reusable))
