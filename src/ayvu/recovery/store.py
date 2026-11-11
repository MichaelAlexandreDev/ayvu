"""Read-only evidence for recovery, over the state files and the project folders.

Nothing here creates, locks, migrates or changes anything: a project is read through its own read-only
connections, the guard is only probed, and a book or an output is only hashed. The state file says how a
translation was invoked, and that is checked against the job's fingerprint before anything believes it.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..artifacts import Artifact, ArtifactError
from ..artifacts.store import SqliteArtifactRepository
from ..jobs import JobStoreError, JobRequest
from .service import (
    RECOVERY_BETWEEN_CHUNKS, InvocationView, OutputEvidence, RecoveryCancelled, RecoveryProgress, RecoveryScan, SourceState,
)
from ..translation import ArtifactIntegrity, RecoveryReason, ResumeStage, StageEvidence
from ..jobs import JobSnapshot
from ..jobs.store import MAX_LIST_LIMIT, SqliteJobRepository
from ..v1.resume_scan import LegacyStateEntry, scan_legacy_states
from ..projects import ProjectError, ProjectManifest, ProjectSourceError
from ..projects.schema import project_migration_runner
from ..projects.store import ConfigurationReferences, ProjectJsonRepository, canonical_source_path, configuration_references, file_digest, source_digest, translation_selections
from ..provenance import ProvenanceError, ProducerIdentity
from ..provenance.store import SqliteProvenanceRepository
from ..v1.resume import RUNNING_STATUS, TranslationResumeState
from ..translation import OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TranslateCommandError, TranslateProjectCommand, resolve_execution_plan
from ..kernel import ChapterSelection, ChapterSelectionError, LanguagePair, OutputPlan, TranslationOptions, JobError, CancellationToken

RAW_RESULTS_ROLE = "raw_results"
_PROVENANCE_PAGE = 64


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _options(state: TranslationResumeState) -> TranslationOptions:
    selection = None if state.chapter_selection is None else ChapterSelection.parse(state.chapter_selection)
    return TranslationOptions(
        language_pair=LanguagePair(source=state.source, target=state.target), fail_fast=state.fail_fast, chunk_limit=state.chunk_limit,
        workers=state.workers, translate_metadata=state.translate_metadata, translate_alt_text=state.translate_alt_text,
        chapter_selection=selection, translation_memory=state.translation_memory_options())


def plan_fingerprints(state: TranslationResumeState, project: ProjectManifest, *,
                      request: JobRequest | None = None, producer: ProducerIdentity | None = None) -> tuple[str, ...]:
    """The plans this file could have run under. The file holds the flag, and the run may have accepted a prompt instead."""
    options = _options(state)
    return tuple(
        resolve_execution_plan(TranslateProjectCommand(project.project_id, project.source_sha256, options, OutputPlan(path=state.output_path),
            overwrite=overwrite, execution_revision=state.execution_revision, producer=producer,
            additional_output_roles=() if request is None else tuple(role for role in request.output_roles if role != OUTPUT_ROLE)),
            project).input_fingerprint
        for overwrite in (False, True))


def _configuration_references(state: TranslationResumeState) -> ConfigurationReferences:
    return configuration_references(
        selections=translation_selections(
            translator=state.translator_name, url=state.url, chunk_limit=state.chunk_limit, translate_metadata=state.translate_metadata,
            translate_alt_text=state.translate_alt_text, chapters=state.chapter_selection, translation_memory=state.translation_memory_options()),
        glossary_path=state.glossary_path, profile_name=state.profile_name)


def _configuration_matches(state: TranslationResumeState, references: ConfigurationReferences, project: ProjectManifest) -> bool:
    return (project.source, project.target) == (state.source, state.target) and (
        project.profile_ref, project.glossary_ref, project.provider_ref, project.configuration_ref) == (
        references.profile_ref, references.glossary_ref, references.provider_ref, references.configuration_ref)


def _progress(state: TranslationResumeState) -> RecoveryProgress:
    return RecoveryProgress(state.total_chapters, len(state.completed_chapters), len(state.failed_chapters), state.failed_segment_count)


@dataclass(frozen=True, repr=False)
class _Association:
    project: ProjectManifest | None = None
    fingerprints: tuple[str, ...] = ()
    problem: RecoveryReason | None = None

    def __repr__(self) -> str:
        return f"_Association(problem={self.problem})"


def _associate(state: TranslationResumeState, projects: tuple[ProjectManifest, ...]) -> _Association:
    """Without reading the book: its digest is what ``source`` checks afterwards, and only when asked."""
    try:
        references = _configuration_references(state)
        canonical = canonical_source_path(state.input_path)
        matching = tuple(project for project in projects
                         if project.source_path == canonical and _configuration_matches(state, references, project))
    except ProjectError:
        # A glossary that is gone, or an address holding credentials: the current flow still has its say on those.
        return _Association(problem=RecoveryReason.MISSING_CONFIGURATION)
    if not matching:
        return _Association()
    if len(matching) > 1:
        return _Association(problem=RecoveryReason.INCOMPATIBLE_PLAN)
    try:
        return _Association(matching[0], plan_fingerprints(state, matching[0]))
    except (ChapterSelectionError, TranslateCommandError, ValueError):
        return _Association(problem=RecoveryReason.INCOMPATIBLE_PLAN)


class LocalRecoveryEvidence:
    """``policy_refusal`` is the same check every other path runs, given here so that this layer knows no format."""

    def __init__(self, processing_dir: Path, *, policy_refusal: Callable[[Path], str | None], clock: Callable[[], datetime] = _utc_now) -> None:
        self._processing_dir = processing_dir
        self._projects = ProjectJsonRepository(processing_dir)
        self._policy_refusal = policy_refusal
        self._clock = clock
        self._runner = project_migration_runner()
        self._legacy: dict[str, LegacyStateEntry] = {}
        self._manifests: tuple[ProjectManifest, ...] = ()

    def __repr__(self) -> str:
        return "LocalRecoveryEvidence()"

    def legacy_entry(self, reference: str) -> LegacyStateEntry | None:
        """The file behind a reference, for the presentation or runner that has to read its paths."""
        if not self._legacy:
            self.scan()
        return self._legacy.get(reference)

    def scan(self) -> RecoveryScan:
        catalog = self._projects.read_catalog()
        self._manifests = catalog.projects
        entries = scan_legacy_states(self._processing_dir)
        self._legacy = {entry.reference: entry for entry in entries}
        return RecoveryScan(tuple(self._view(entry) for entry in entries),
                            tuple(project.project_id for project in catalog.projects), len(catalog.failures))

    def _view(self, entry: LegacyStateEntry) -> InvocationView:
        state = entry.state
        if state is None:
            problem = RecoveryReason.UNSUPPORTED_SCHEMA if entry.failure == "legacy_state_unsupported_version" else RecoveryReason.INVALID_STATE_FILE
            return InvocationView(entry.reference, False, problem=problem, unusable=True)
        association = _associate(state, self._manifests)
        project_id = None if association.project is None else association.project.project_id
        if state.execution_revision != 1:
            # A non-legacy checkpoint must never fall back to a project-free v1 invocation.
            fingerprints = self._recorded_fingerprints(state, association.project)
            return InvocationView(entry.reference, state.status == RUNNING_STATUS, _progress(state), project_id,
                fingerprints, None if fingerprints else RecoveryReason.INCOMPATIBLE_PLAN, unusable=not bool(fingerprints))
        return InvocationView(entry.reference, state.status == RUNNING_STATUS, _progress(state), project_id, association.fingerprints, association.problem)

    def _recorded_fingerprints(self, state: TranslationResumeState, project: ProjectManifest | None) -> tuple[str, ...]:
        """Check the checkpoint against job-bound producer evidence, never today's configuration."""
        if project is None:
            return ()
        try:
            paths = self._projects.read_database_paths(project.project_id)
            provenance = SqliteProvenanceRepository(self._runner, paths.database, project.project_id, clock=self._clock)
            fingerprints = []
            for snapshot in self.jobs(project.project_id):
                job = snapshot.job
                request = job.request
                if request.command_type != TRANSLATE_COMMAND_TYPE or request.command_version != state.execution_revision:
                    continue
                nodes = provenance.list_by_job(job.job_id, limit=_PROVENANCE_PAGE).nodes
                producers = {node.record.request.producer for node in nodes if node.record.request.producer.fully_identified}
                if len(producers) != 1:
                    continue
                candidates = plan_fingerprints(state, project, request=request, producer=producers.pop())
                if request.input_fingerprint in candidates:
                    fingerprints.append(request.input_fingerprint)
            return tuple(dict.fromkeys(fingerprints))
        except (ProjectError, JobError, ProvenanceError, OSError, sqlite3.Error):
            return ()

    def _manifest(self, project_id: str | None) -> ProjectManifest | None:
        return next((project for project in self._manifests if project.project_id == project_id), None)

    def _state(self, reference: str) -> TranslationResumeState | None:
        entry = self._legacy.get(reference)
        return None if entry is None else entry.state

    def jobs(self, project_id: str) -> tuple[JobSnapshot, ...]:
        try:
            paths = self._projects.read_database_paths(project_id)
        except ProjectError:
            raise JobStoreError("project_unavailable") from None
        if not paths.database.exists():
            return ()
        repository = SqliteJobRepository(self._runner, paths.database, project_id, clock=self._clock)
        return repository.list_jobs(project_id, limit=MAX_LIST_LIMIT, newest_first=True)

    def run_active(self, project_id: str, input_fingerprint: str) -> bool:
        try:
            return self._projects.read_run_guard(project_id).held(input_fingerprint)
        except ProjectError:
            # A guard that cannot be read cannot clear anyone. Say it is busy.
            return True

    @staticmethod
    def _checkpoint(token: CancellationToken | None) -> Callable[[], None] | None:
        if token is None:
            return None

        def between_chunks() -> None:
            if token.checkpoint(RECOVERY_BETWEEN_CHUNKS) is not None:
                raise RecoveryCancelled
        return between_chunks

    def source(self, reference: str, project_id: str | None, token: CancellationToken | None) -> SourceState:
        state = self._state(reference)
        if state is None:
            return SourceState.UNKNOWN
        try:
            size, digest = source_digest(state.input_path, between_chunks=self._checkpoint(token))
        except ProjectSourceError as error:
            return SourceState.MISSING if str(error) == "source_missing" else SourceState.UNREADABLE
        except ProjectError:
            return SourceState.UNREADABLE
        project = self._manifest(project_id)
        if project is not None:
            return SourceState.SAME if (project.source_size, project.source_sha256) == (size, digest) else SourceState.CHANGED
        # Its own project is not known, but another one may have recorded this path, and translating would refuse the same.
        try:
            canonical = canonical_source_path(state.input_path)
        except ProjectError:
            return SourceState.UNKNOWN
        recorded = tuple(item for item in self._manifests if item.source_path == canonical)
        if recorded and not any((item.source_size, item.source_sha256) == (size, digest) for item in recorded):
            return SourceState.CHANGED
        return SourceState.UNKNOWN

    def policy_refusal(self, reference: str, project_id: str | None) -> str | None:
        state = self._state(reference)
        return None if state is None else self._policy_refusal(state.input_path)

    def _artifacts(self, project_id: str, job_id: str) -> tuple[Artifact, ...]:
        try:
            paths = self._projects.read_database_paths(project_id)
            if not paths.database.exists():
                return ()
            return SqliteArtifactRepository(self._runner, paths.database, project_id, clock=self._clock).list_ready(job_id)
        except (ProjectError, ArtifactError, JobError, OSError, sqlite3.Error):
            return ()

    def output(self, reference: str, project_id: str | None, job_id: str | None, token: CancellationToken | None) -> OutputEvidence:
        state = self._state(reference)
        if state is None:
            return OutputEvidence()
        record = None
        if project_id is not None and job_id is not None:
            record = next((item for item in self._artifacts(project_id, job_id) if item.request.role == OUTPUT_ROLE), None)
        try:
            size, digest = file_digest(state.output_path, between_chunks=self._checkpoint(token))
        except ProjectSourceError as error:
            exists = str(error) != "source_missing"
            if record is not None:
                return OutputEvidence(ArtifactIntegrity.MISSING if not exists else ArtifactIntegrity.CORRUPT, exists=exists)
            return OutputEvidence(conflict=exists, exists=exists)
        except ProjectError:
            return OutputEvidence(ArtifactIntegrity.CORRUPT if record is not None else None, conflict=record is None, exists=True)
        if record is None:
            return OutputEvidence(conflict=True, exists=True)
        if (size, f"sha256:{digest}") == (record.size, record.digest):
            return OutputEvidence(ArtifactIntegrity.VERIFIED, exists=True)
        return OutputEvidence(ArtifactIntegrity.PARTIAL if size < record.size else ArtifactIntegrity.CORRUPT, exists=True)

    def producer_identified(self, project_id: str, job_id: str) -> bool:
        """Legacy records remain unknown. Declared rich producers are identified; no record proves nothing."""
        try:
            paths = self._projects.read_database_paths(project_id)
            if not paths.database.exists():
                return False
            nodes = SqliteProvenanceRepository(self._runner, paths.database, project_id, clock=self._clock).list_by_job(job_id, limit=_PROVENANCE_PAGE).nodes
        except (ProjectError, ProvenanceError, JobError, OSError, sqlite3.Error):
            return False
        return bool(nodes) and all(node.record.request.producer.fully_identified for node in nodes)

    def stages(self, project_id: str, job_id: str) -> tuple[StageEvidence, ...]:
        """What the records say. Nothing records what a reuse would be compared with yet, so no bytes need reading."""
        artifacts = self._artifacts(project_id, job_id)

        def evidence(stage: ResumeStage, role: str) -> StageEvidence:
            found = any(item.request.role == role for item in artifacts)
            return StageEvidence(stage, ArtifactIntegrity.UNCHECKED if found else ArtifactIntegrity.MISSING)

        return (evidence(ResumeStage.TRANSLATE, RAW_RESULTS_ROLE), evidence(ResumeStage.RECONSTRUCT, OUTPUT_ROLE))
