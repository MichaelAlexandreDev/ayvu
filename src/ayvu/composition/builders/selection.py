"""Select existing translator adapters and models without changing persisted identities (#377)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from ...projects import ProjectError
from ...provenance import ProvenanceError
from ...kernel import JobError

from ...preferences import AyvuConfig, TranslationProfile, MODEL_TRANSLATORS
from ...projects.schema import project_migration_runner
from ...projects.store import ProjectJsonRepository
from ...provenance.store import SqliteProvenanceRepository
from ...translation import PreflightError
from .rich_provider import require_model
from .ollama import require_supported_mode


@dataclass(frozen=True, repr=False)
class TranslatorSelection:
    translator: str
    model: str | None
    url: str
    config: AyvuConfig


def select_translator(config: AyvuConfig, *, translator: str | None = None, model: str | None = None,
                      profile: TranslationProfile | None = None, url: str | None = None,
                      dry_run: bool = False, cache_only: bool = False, preview: bool = False) -> TranslatorSelection:
    """Explicit fields override the named profile, then the translator's configured model.

    A profile model belongs to its translator; switching translators alone never borrows that model.
    Global configuration holds one model per translator, never an implicit default translator.
    """
    profile_translator = profile.translator if profile else None
    name = translator if translator is not None else profile_translator or 'libretranslate'
    name = name.strip().lower()
    if name not in MODEL_TRANSLATORS:
        # Keep the existing specific unsupported-translator diagnostic before any resource is opened.
        require_model({}, name)
    if name == 'ollama':
        require_supported_mode(dry_run=dry_run, cache_only=cache_only, preview=preview)
    profile_model = profile.model if profile and profile_translator in (None, name) else None
    chosen = model if model is not None else profile_model if profile_model is not None else config.translator_models.get(name)
    if chosen is not None and not MODEL_TRANSLATORS[name](chosen):
        raise PreflightError('Invalid translator model.', 'Use a valid model for the selected translator.')
    models = dict(config.translator_models)
    if chosen is not None:
        models[name] = chosen
    endpoint = url if url is not None else ('http://localhost:11434' if name == 'ollama' else 'http://localhost:5000')
    return TranslatorSelection(name, chosen, endpoint, replace(config, translator_models=models))


def recorded_producer(processing_dir, project_id, ticket):
    """Read the job's existing provenance; no checkpoint field, migration or store creation."""
    try:
        paths = ProjectJsonRepository(processing_dir).read_database_paths(project_id)
        repository = SqliteProvenanceRepository(project_migration_runner(), paths.database, project_id)
        nodes = repository.list_by_job(ticket.job_id, limit=64).nodes
    except (ProjectError, ProvenanceError, JobError, OSError, sqlite3.Error):
        raise PreflightError('The original translator model could not be recovered.',
                             'Keep the job provenance and start a new translation if it is unavailable.') from None
    producers = {node.record.request.producer for node in nodes if node.record.request.producer.fully_identified}
    if len(producers) != 1:
        raise PreflightError('The original translator model could not be recovered.',
                             'Keep the job provenance and start a new translation if it is unavailable.')
    return producers.pop()


def resume_configuration(config, processing_dir, project_id, ticket):
    producer = recorded_producer(processing_dir, project_id, ticket)
    if producer.provider == 'libretranslate' and producer.model.startswith('declared:'):
        model = producer.model.removeprefix('declared:')
    elif producer.provider == 'ollama' and '@' in producer.model:
        model = producer.model.rsplit('@', 1)[0]
    else:
        raise PreflightError('The original translator model is unsupported.', 'Start a new translation.')
    return select_translator(config, translator=producer.provider, model=model).config


def require_resume_producer(book, producer):
    if producer != recorded_producer(book.processing_dir, book.project_id, book.continuation):
        raise PreflightError('The installed model changed since this job started.',
                             'Restore the original model weights or start a new translation.')
