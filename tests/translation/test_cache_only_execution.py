"""Offline execution through real cache stores and the shared linguistic kernel."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID
import sqlite3

import pytest

from ayvu.content import AdapterIdentity, Locator, ProtectedPart, SemanticKind, UnitContent, build_document, UNKNOWN_LOCALE
from ayvu.knowledge.cache import CacheAuthorizationPolicy, CacheIdentityError, CacheOnlyRequest, PinnedExecutionIdentityReference, ResolvedCacheExecutionIdentity, TRANSLATION_STAGE_AGGREGATE
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.providers import ProviderRoute
from ayvu.translation import TranslationExecutionError
from tests.support.rich_cache_only_support import warm_units, executor, offline_guard, document, Env, make_plan, Glossed
from tests.support.transient_translation_support import exact_entry
from tests.knowledge.cache.read_only_support import snapshot


def test_hits_reapply_glossary_preserve_cache_and_record_exact_references(tmp_path, monkeypatch):
    doc = document("  Hello reader.  ", "Goodbye reader.")
    path = tmp_path / "cache.sqlite"
    plan, env, transport = warm_units(path, (doc,))
    assert len(transport.posts) == 2
    before = snapshot(tmp_path)
    env.provenance.calls.clear()
    subject = executor(path, plan, env, glossary=lambda value: Glossed(value.replace("reader", "CURRENT")))
    with offline_guard(monkeypatch):
        subject.prepare_document(doc)
        results = [subject.execute(unit) for unit in doc.units]
    assert all(result.status == "cache" and result.missing_text is None for result in results)
    assert results[0].translated == "  PT:Hello CURRENT.  "
    assert snapshot(tmp_path) == before
    assert len(env.provenance.calls) == 2
    assert all(call.operation == "reuse_exact_cache" and call.source_kind.value == "cache"
               and call.configuration_refs[0].reference.startswith("cache_entry:") for call in env.provenance.calls)


def test_absent_database_is_missing_without_creating_any_file(tmp_path, monkeypatch):
    doc, env = document("One", "One"), Env()
    subject = executor(tmp_path / "absent" / "cache.sqlite", env.plan, env)
    with offline_guard(monkeypatch):
        subject.prepare_document(doc)
    assert [subject.execute(unit).missing_text for unit in doc.units] == ["One", "One"]
    assert list(tmp_path.iterdir()) == [] and env.provenance.calls == []


@pytest.mark.parametrize("dimension", ["model", "chunk_limit", "revision", "ambiguous", "unsupported"])
def test_incompatible_or_ambiguous_identity_is_missing(tmp_path, monkeypatch, dimension):
    doc, path = document("Hello"), tmp_path / "cache.sqlite"
    plan, env, _ = warm_units(path, (doc,))
    if dimension == "ambiguous":
        writer = SQLiteExactCache(path)
        other = replace(plan, capabilities="capfp:v1:" + "d" * 64, route_hops=None)
        writer.put_if_absent(exact_entry(SimpleNamespace(project_id=doc.project_id), other, "Hello", "Other"))
    if dimension == "model":
        plan = replace(plan, producer=replace(plan.producer, model="declared:changed"), route_hops=None)
    if dimension == "chunk_limit":
        plan = replace(plan, chunk_limit=4000)
    if dimension == "revision":
        from ayvu.knowledge.cache import OpaqueSemanticRevision
        changed = replace(plan, normalization_revision=OpaqueSemanticRevision("translation", "normalization", 1, "changed-normalization"))
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM cache_entries_v2")
        SQLiteExactCache(path).put_if_absent(exact_entry(SimpleNamespace(project_id=doc.project_id), changed, "Hello", "Other"))
    subject = executor(path, plan, env)
    if dimension == "unsupported":
        from ayvu.translation import LocalCachePlanResolver
        subject.units.resolver = LocalCachePlanResolver(subject.units.reader, subject.scope,
            replace(subject.units.resolver._intent, context_ref="context:" + "a" * 64, context_strategy="configured_context"))
    with offline_guard(monkeypatch):
        subject.prepare_document(doc)
    result = subject.execute(doc.units[0])
    assert result.status == "missing" and result.missing_text == "Hello"


def test_each_unit_recovers_its_own_bridge_or_direct_plan(tmp_path, monkeypatch):
    doc, path = document("One", "Two"), tmp_path / "cache.sqlite"
    plan, env, _ = warm_units(path, (doc,))
    # Keep only the first direct entry, then store a distinct bridge for the second unit.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM cache_entries_v2 WHERE source_text='Two'")
    bridge = replace(plan, route=ProviderRoute("en", "pt", "es"), route_hops=None)
    SQLiteExactCache(path).put_if_absent(exact_entry(SimpleNamespace(project_id=doc.project_id), bridge, "Two", "Bridge"))
    subject = executor(path, plan, env)
    with offline_guard(monkeypatch):
        subject.prepare_document(doc)
    assert [subject.execute(unit).translated for unit in doc.units] == ["PT:One", "Bridge"]


@pytest.mark.parametrize("problem,code", [("checksum", "cache_entry_corrupt"), ("tokens", "cache_entry_corrupt"),
                                         ("overflow", "cache_candidate_limit_exceeded")])
def test_fatal_evidence_never_produces_unit_execution(tmp_path, monkeypatch, problem, code):
    doc, path = document("First", "Second"), tmp_path / "cache.sqlite"
    plan, env, _ = warm_units(path, (doc,))
    if problem == "checksum":
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE cache_entries_v2 SET value='BROKEN' WHERE source_text='Second'")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM cache_entries_v2 WHERE source_text='Second'")
        writer = SQLiteExactCache(path)
        for index in range(257 if problem == "overflow" else 1):
            candidate = replace(plan, capabilities="capfp:v1:" + f"{index:064x}", route_hops=None)
            writer.put_if_absent(exact_entry(SimpleNamespace(project_id=doc.project_id), candidate, "Second",
                "__AYVU_TAG_99__" if problem == "tokens" else "Second"))
    before = snapshot(tmp_path)
    subject = executor(path, plan, env)
    with offline_guard(monkeypatch), pytest.raises(TranslationExecutionError) as failure:
        subject.prepare_document(doc)
    assert failure.value.failure.code == code and subject._executions == {}
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("pin_state", ["absent", "mismatch", "foreign", "valid"])
def test_pins_require_durable_authority_and_never_fall_back(tmp_path, monkeypatch, pin_state):
    doc, path = document("Hello"), tmp_path / "cache.sqlite"
    plan, env, _ = warm_units(path, (doc,))
    scope = CacheAuthorizationPolicy(doc.project_id).resolve()
    if pin_state == "foreign":
        scope = CacheAuthorizationPolicy(str(UUID(int=999))).resolve()
    pin = ResolvedCacheExecutionIdentity(scope, TRANSLATION_STAGE_AGGREGATE,
        "sha256:" + "e" * 64 if pin_state == "mismatch" else plan.semantic_fingerprint)
    kwargs = dict(request=CacheOnlyRequest.pinned(PinnedExecutionIdentityReference("job:proven")),
        resolve_pin=None if pin_state == "absent" else lambda reference, authority: pin)
    with offline_guard(monkeypatch):
        if pin_state == "foreign":
            with pytest.raises(CacheIdentityError, match="cache_scope_denied"):
                executor(path, plan, env, **kwargs)
            return
        subject = executor(path, plan, env, **kwargs)
        subject.prepare_document(doc)
    assert subject.execute(doc.units[0]).status == ("cache" if pin_state == "valid" else "missing")


@pytest.mark.parametrize("kind", ["paragraph", "title", "alt_text"])
def test_missing_original_is_readable_and_keeps_literal_tokens_unicode_and_spaces(tmp_path, monkeypatch, kind):
    value = "  Café <b>λ</b> __AYVU_TAG_7__ HTTP  "
    spans = tuple(ProtectedPart(value.index(part), value.index(part) + len(part), "markup" if part.startswith("<") else "placeholder")
                  for part in ("<b>", "</b>", "__AYVU_TAG_7__"))
    unit = UnitContent(Locator("paragraph", 1, "p"), value, SemanticKind(kind), (1,), protected_parts=spans)
    env = Env()
    doc = build_document(env.job.request.project_id, "source.xhtml", AdapterIdentity("html", 1, 1), UNKNOWN_LOCALE, (unit,))
    subject = executor(tmp_path / "absent.sqlite", env.plan, env, glossary=lambda value: Glossed(value.replace("HTTP", "WRONG")))
    with offline_guard(monkeypatch):
        subject.prepare_document(doc)
    result = subject.execute(doc.units[0])
    expected = value if kind in {"title", "alt_text"} else "Café λ __AYVU_TAG_7__ HTTP"
    assert result.missing_text == expected and result.reconstruction_text != ""
    assert "__AYVU_TAG_0__" not in result.missing_text


def test_empty_missing_original_is_rejected(tmp_path, monkeypatch):
    doc, env = document("Hello"), Env()
    subject = executor(tmp_path / "absent.sqlite", env.plan, env, readable=lambda *_: "")
    with offline_guard(monkeypatch), pytest.raises(TranslationExecutionError) as failure:
        subject.prepare_document(doc)
    assert failure.value.failure.code == "empty_missing_original" and not subject._executions


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_real_format_engine_reconstructs_or_gates_before_publication(tmp_path, monkeypatch, warm, full):
    import io
    from zipfile import ZipFile
    from ayvu.artifacts import ArtifactRequest
    from ayvu.formats import ContentScope, ExtractionRequest, FormatPublication
    from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
    from ayvu.formats.epub.reader import read_package
    from ayvu.formats.epub.validation import validate_output_epub
    from ayvu.translation import TranslationEngine, EngineRequest
    from tests.support.rich_cache_only_support import _book, ready_publisher
    from ayvu.composition.builders.translate_project import epub_format_service
    source = _book(tmp_path / "book.epub")
    env, path = Env(), tmp_path / "cache.sqlite"
    service = epub_format_service()
    documents = service.extract("epub", LocalFormatSource(source),
        ExtractionRequest(env.job.request.project_id, frozenset({ContentScope.BODY}))).documents
    if warm:
        plan, env, _ = warm_units(path, documents)
    else:
        plan = env.plan
    env.artifacts.calls.clear()
    subject = executor(path, plan, env)
    request = ArtifactRequest(env.job.request.project_id, env.job.job_id, "translated_output", "reconstruct", 1,
        "sha256:" + "c" * 64, "application/epub+zip")
    output = tmp_path / "out.epub"
    output.write_bytes(b"previous")
    output.chmod(0o640)
    before = source.read_bytes(), output.read_bytes(), output.stat().st_mode
    publication = FormatPublication(ready_publisher(env.artifacts), request, env.job.lease.owner_token, LocalOutputGuard(output))
    engine = TranslationEngine(service, subject, prepare_document=subject.prepare_document)
    with offline_guard(monkeypatch):
        result = engine.run(EngineRequest("epub", LocalFormatSource(source), publication, env.job.request.project_id,
            read_package(source).facts, require_full_cache=full))
    assert result.failure is None
    assert result.stats.missing == (0 if warm else 5)
    assert result.stats.from_cache == (5 if warm else 0)
    assert result.output_written == (warm or not full)
    assert (source.read_bytes(), output.read_bytes(), output.stat().st_mode) == before
    if full and not warm:
        assert env.artifacts.calls == []
        assert result.stats.missing_texts == ["T", "Hello title", "Hello reader.", "T", "Goodbye reader."]
    else:
        assert len(env.artifacts.calls) == 1
        written_request, payload = env.artifacts.calls[0]
        assert written_request.role == "translated_output"
        with ZipFile(io.BytesIO(payload)) as archive:
            assert (b"PT:Hello reader." in archive.read("OEBPS/c1.xhtml")) is warm
        staged = tmp_path / "staged.epub"
        staged.write_bytes(payload)
        assert validate_output_epub(staged).ok


def test_corruption_in_a_later_document_never_calls_reconstruction(tmp_path, monkeypatch):
    from ayvu.translation import TranslationEngine, EngineRequest
    from ayvu.formats import ContentScope, ExtractionRequest
    from ayvu.formats.local import LocalFormatSource
    from ayvu.formats.epub.reader import read_package
    from ayvu.composition.builders.translate_project import epub_format_service
    from tests.support.rich_cache_only_support import _book
    source, env, path = _book(tmp_path / "book.epub"), Env(), tmp_path / "cache.sqlite"
    service = epub_format_service()
    docs = service.extract("epub", LocalFormatSource(source),
        ExtractionRequest(env.job.request.project_id, frozenset({ContentScope.BODY}))).documents
    plan, env, _ = warm_units(path, docs)
    env.provenance.calls.clear()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cache_entries_v2 SET value='broken' WHERE source_text=?", (docs[-1].units[-1].source_text,))
    monkeypatch.setattr(service, "reconstruct", lambda *args, **kwargs: pytest.fail("corruption reconstructed"))
    subject = executor(path, plan, env)
    engine = TranslationEngine(service, subject, prepare_document=subject.prepare_document)
    with offline_guard(monkeypatch), pytest.raises(TranslationExecutionError) as failure:
        engine.run(EngineRequest("epub", LocalFormatSource(source), None, env.job.request.project_id, read_package(source).facts))
    assert failure.value.failure.code == "cache_entry_corrupt"
    assert env.provenance.calls and all(call.operation == "reuse_exact_cache" for call in env.provenance.calls)


@pytest.mark.parametrize("lease", ["expired", "absent", "foreign"])
def test_durable_authority_is_required_before_any_lookup(tmp_path, monkeypatch, lease):
    from datetime import timedelta
    env, doc = Env(), document("Hello")
    if lease == "expired":
        env.job = replace(env.job, lease=replace(env.job.lease, expires_at=env.job.created_at + timedelta(seconds=1)))
    elif lease == "absent":
        env.job = replace(env.job, lease=None)
    else:
        doc = build_document(str(UUID(int=999)), doc.logical_key, doc.adapter, doc.source_locale,
            tuple(unit.content for unit in doc.units))
    subject = executor(tmp_path / "absent.sqlite", env.plan, env)
    if lease == "expired":
        subject.clock = lambda: env.job.created_at + timedelta(minutes=20)
    with offline_guard(monkeypatch), pytest.raises(TranslationExecutionError):
        subject.prepare_document(doc)
    assert subject._executions == {} and env.provenance.calls == []
