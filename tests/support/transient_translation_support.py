"""Real local evidence and strict effect guards for the transient route."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys

import pytest
import requests

from ayvu.composition import cli as composition
from ayvu.composition.builders import translate_transient as builder
from ayvu.composition.builders.translate_project import epub_format_service, v2_cache_path
from ayvu.content import TextParts
from ayvu.formats import ContentScope, ExtractionRequest
from ayvu.formats.local import LocalFormatSource
from ayvu.knowledge.cache import CacheAuthorizationPolicy, CacheProvenanceMetadata, ExactCacheEntry, exact_entry_key, TRANSLATION_STAGE_AGGREGATE
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.glossary import apply_glossary_with_usage, effective_glossary_reference
from ayvu.formats.epub.reader import read_package
from ayvu.formats.epub import review_text_from_template
from ayvu.translation.preflight import run_local_translation_preflight
from ayvu.projects.store import ProjectJsonRepository
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
from ayvu.translation import LocalCachePlanIntent, LocalCachePlanResolver, TransientExecution, TransientTranslation, TransientUnits, to_report
from ayvu.translation.document_contracts import _template
from tests.support.cli_translate_project_support import _book, _args, runner, app
from tests.support.cli_rich_runtime_support import config, isolate, text
from tests.support.translation_cache_v2_support import make_plan
from tests.knowledge.cache.read_only_support import snapshot


def prepared(root, source, options, *, glossary=None, profile=None):
    local = run_local_translation_preflight(epub_path=source, language_pair=options.language_pair, glossary_path=glossary,
        translator_name="libretranslate", url="http://localhost:5000", timeout=30, retries=0,
        requests_per_second=None, retry_backoff=0.5, retry_backoff_max=8)
    return builder.prepare(epub_path=source, options=options, glossary_path=glossary, glossary=local.glossary,
        translator_name="libretranslate", url="http://localhost:5000",
        processing_dir=root / "processing", profile_name=profile)


def seed(root, source, options, *, warm=True, glossary=None, project=None, plan_changes=None):
    prep = prepared(root, source, options, glossary=glossary)
    project = project or ProjectJsonRepository(root / "processing").create(prep.request)
    cache_path = v2_cache_path(root / "cache.sqlite")
    writer = SQLiteExactCache(cache_path)
    plan = make_plan(producer=replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026"),
                     chunk_limit=options.chunk_limit, **(plan_changes or {}))
    scope = CacheAuthorizationPolicy(project.project_id).resolve()
    extraction = epub_format_service().extract("epub", LocalFormatSource(source),
        ExtractionRequest(project.project_id, frozenset({ContentScope.BODY})))
    assert extraction.failure is None
    if warm:
        for document in extraction.documents:
            for unit in document.units:
                template, _ = _template(unit)
                value = "PT:" + TextParts.from_text(template).core
                writer.put_if_absent(exact_entry(project, plan, unit.source_text, value))
    return project, cache_path


def exact_entry(project, plan, source, value):
    scope = CacheAuthorizationPolicy(project.project_id).resolve()
    key = exact_entry_key(scope, TRANSLATION_STAGE_AGGREGATE, plan.semantic_fingerprint, source)
    return ExactCacheEntry(key, value, CacheProvenanceMetadata("translate", plan.producer, plan.pipeline_revision),
                           source, plan.semantic_operation.canonical_payload())


def forbidden(*args, **kwargs):
    pytest.fail("forbidden network, durable execution or publication")


@contextmanager
def effect_guard(monkeypatch, root, *, presentation=False):
    """Every invocation snapshots the tree and captures SQL; no writer is allowed."""
    before, statements, connections = snapshot(root), [], []
    original = sqlite3.connect
    def connect(database, *args, **kwargs):
        assert kwargs.get("uri") and str(database).endswith("?mode=ro")
        connection = original(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection
    with monkeypatch.context() as patch:
        deny_runtime(patch)
        patch.setattr(sqlite3, "connect", connect)
        yield statements
    after = snapshot(root)
    if presentation:
        # Renderers may add reports and their directories, never mutate inputs/state.
        for name, value in before.items():
            assert name in after
            if value[-1] is not None:
                assert after[name] == value, name
        new_files = [name for name in after.keys() - before.keys() if after[name][-1] is not None]
        assert all(Path(name).suffix in {".txt", ".md"} for name in new_files), new_files
    else:
        assert after == before
    assert not any(re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|REPLACE|VACUUM|ATTACH|DETACH)\b|PRAGMA\s+\w+\s*=|BEGIN\s+IMMEDIATE", sql, re.I)
                   for sql in statements), statements
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def deny_runtime(patch):
    for name in ("getaddrinfo", "gethostbyname", "create_connection"):
        patch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex", "sendto", "send", "sendall"):
        patch.setattr(socket.socket, name, forbidden)
    patch.setattr(requests, "Session", forbidden)
    for target in (
        "ayvu.providers.libretranslate.client.LibreTranslateTranslator.__init__",
        "ayvu.providers.libretranslate.client.create_translator",
        "ayvu.translation.preflight.create_translator", "ayvu.translation.preflight.CliProviderSession",
        "ayvu.providers.session.CliProviderSession.__init__",
        "ayvu.providers.libretranslate.adapter.LibreTranslateProvider.__init__",
        "ayvu.translation.DocumentTranslator.__init__", "ayvu.translation.TranslationEngine.__init__",
        "ayvu.knowledge.cache.repository.SQLiteExactCache.__init__",
        "ayvu.knowledge.cache.repository.SQLiteExactCache.put_if_absent",
        "ayvu.projects.store.ProjectJsonRepository.create", "ayvu.projects.store.ProjectJsonRepository.resolve",
        "ayvu.projects.store.ProjectJsonRepository.load", "ayvu.projects.store.ProjectJsonRepository.list_projects",
        "ayvu.projects.schema.open_job_repository", "ayvu.projects.schema.open_artifact_service",
        "ayvu.projects.schema.open_provenance_repository", "ayvu.jobs.store.SqliteJobRepository.__init__",
        "ayvu.recovery.checkpoint.ResumeStateStore.__init__", "ayvu.translation.project_lease._LeaseKeeper.__init__",
        "ayvu.artifacts.store.SqliteArtifactRepository.__init__", "ayvu.artifacts.destination.DestinationPublications.__init__",
        "ayvu.artifacts.store.ArtifactFileOps.__init__", "ayvu.artifacts.ArtifactService.__init__",
    ):
        patch.setattr(target, forbidden)


@contextmanager
def active_writer(path):
    code = """
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('BEGIN IMMEDIATE')
c.execute('PRAGMA user_version=42')
print('ready', flush=True)
sys.stdin.readline()
c.rollback()
c.close()
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert path.with_name(path.name + "-journal").exists()
        yield process
    finally:
        process.communicate("release\n", timeout=10)
        assert process.returncode == 0


def invoke(root, source, *args):
    return runner.invoke(app, _args(root, source, "--dry-run", *args))


def execute(root, source, options, project=None, *, glossary=None, observer=None, cancellation=None):
    prep = prepared(root, source, options, glossary=glossary)
    reader, resolver = None, None
    if project is not None:
        reader = ReadOnlyExactCache(v2_cache_path(root / "cache.sqlite"))
        intent = LocalCachePlanIntent(options.source, options.target,
            replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026"), options.chunk_limit,
            glossary_ref=effective_glossary_reference(prep.glossary))
        resolver = LocalCachePlanResolver(reader, CacheAuthorizationPolicy(project.project_id).resolve(), intent)
    units = TransientUnits(options.language_pair, lambda text: apply_glossary_with_usage(text, prep.glossary),
                           review_text_from_template, reader=reader, resolver=resolver, cache_only=options.cache_only, chunk_limit=options.chunk_limit)
    service = TransientTranslation(epub_format_service(), units, cancellation=cancellation,
                                    **({"observer": observer} if observer is not None else {}))
    package = read_package(source)
    result = service.execute(TransientExecution(LocalFormatSource(source), options, package.facts,
                                                 project.project_id if project else None))
    return to_report(result, source, root / "out.epub", package, options, prep.glossary)
