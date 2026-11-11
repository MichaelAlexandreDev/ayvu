"""Opt-in composition contracts. Storage integration is in test_cli_rich_runtime."""
from __future__ import annotations

import ast
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

import ayvu.composition.builders.translate_project as composition
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.cache_identity import CacheEligibilityState, CacheIneligibleReason, PutIfAbsentState
from ayvu.cache_schema import SchemaState, classify_cache
from ayvu.preferences.config_facade import ConfigStore
from ayvu.preferences.model import AyvuConfig, config_from_preferences, config_to_preferences
from ayvu.glossary import load_glossary
from ayvu.job_events import encode_event
from ayvu.job_execution import CancellationSource
from ayvu.preferences.model import GlobalPreferences, PreferencesError, preferences_from_mapping, preferences_to_mapping
from ayvu.provenance.model import ProducerIdentity, ProvenanceValidationError, declared_model
from ayvu.provider import ProviderRoute
from ayvu.provider_capabilities import Capability, ProviderRequirements
from ayvu.provider_libretranslate import LIBRETRANSLATE_PRODUCER, LIBRETRANSLATE_SAFE_POINTS, libretranslate_snapshot
from ayvu.translate_project import RuntimeContext
from ayvu.translation_document import DocumentTranslator, PIPELINE_SAFE_POINTS, TranslationExecutionError
from ayvu.translation_plan import ResolvedTranslationPlan, resolve_plan
from ayvu.v1.translator import LibreTranslateTranslator
from test_cache_repository_v2 import entry
from test_cli_translate_project import Session
from test_translation_cache_v2 import Env, NOW, document
from ayvu.kernel import LanguagePair, TranslationMemoryOptions, TranslationOptions, OutputPlan, JobFailure, JobFailureFamily
from ayvu.translation import BookRefused

PAIR = LanguagePair(source="en", target="pt")


@pytest.mark.parametrize("label", ["a", "model-2026_11", "0", "a" * 48])
def test_declared_label_roundtrips_existing_preferences_in_memory(label):
    assert declared_model(label) == "declared:" + label
    original = AyvuConfig.from_dict({"translator_models": {"libretranslate": label}})
    preferences = config_to_preferences(original)
    parsed, unknown = preferences_from_mapping(preferences_to_mapping(preferences))
    loaded = config_from_preferences(parsed)
    assert not unknown and loaded.translator_models == original.translator_models
    updated = replace(loaded, reader_app="reader")
    assert AyvuConfig.from_dict(updated.to_dict()).translator_models == original.translator_models


@pytest.mark.parametrize("label", ["a", "model-2026_11", "0", "a" * 48])
def test_declared_label_is_validated_namespaced_and_persisted(tmp_path, label):
    config = AyvuConfig(translator_models={"libretranslate": label})
    store = ConfigStore(tmp_path / "config.json")
    store.save(config)
    loaded = store.load()
    assert loaded.translator_models == config.translator_models
    assert json.loads(store.path.read_text())["translator_models"] == config.translator_models
    assert config_from_preferences(config_to_preferences(loaded)).translator_models == config.translator_models
    producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model(label))
    assert producer.model == "declared:" + label and producer.fully_identified
    assert "translator_models" not in repr(loaded) and "declared:<redacted>" in repr(producer)


@pytest.mark.parametrize("label", ["", "A", "a" * 49, "two words", "https://server/model", "/tmp/model", "../model",
                                 "a.b", "a:b", "a@b", "a\nb", "árvore", 12, None])
def test_invalid_labels_are_bounded_errors_without_caller_data(label):
    with pytest.raises(ProvenanceValidationError, match="^invalid_model_label$"):
        declared_model(label)
    with pytest.raises(PreferencesError, match="^invalid:translator_models$"):
        preferences_from_mapping({"translator_models": {"libretranslate": label}})


def test_namespace_is_only_accepted_for_the_model_and_unknown_stays_ineligible():
    with pytest.raises(ProvenanceValidationError):
        ProducerIdentity("declared:model", "model", "adapter", "pipeline")
    with pytest.raises(ProvenanceValidationError):
        replace(LIBRETRANSLATE_PRODUCER, model="observed:model")
    plan = ResolvedTranslationPlan(LIBRETRANSLATE_PRODUCER, ProviderRoute("en", "pt"), "capfp:v1:" + "a" * 64, 3000)
    assert plan.cache_eligibility.reason is CacheIneligibleReason.UNKNOWN_MODEL
    assert plan.semantic_fingerprint is None
    a = replace(plan, producer=replace(LIBRETRANSLATE_PRODUCER, model=declared_model("a")), route_hops=None)
    b = replace(a, producer=replace(a.producer, model=declared_model("b")), route_hops=None)
    assert a.cache_eligibility.state is CacheEligibilityState.ELIGIBLE
    assert a.semantic_fingerprint != b.semantic_fingerprint
    assert replace(b, producer=a.producer, route_hops=None).semantic_fingerprint == a.semantic_fingerprint


def test_absence_keeps_the_existing_configuration_payload_and_runtime_selection():
    assert "translator_models" not in AyvuConfig().to_dict()
    assert "translator_models" not in preferences_to_mapping(GlobalPreferences())
    assert composition.selected_model({}, "libretranslate") is None
    assert composition.selected_model({"libretranslate": "local-v1"}, " LIBRETRANSLATE ") == "local-v1"
    assert composition.selected_model({"libretranslate": "local-v1"}, "other") is None


@pytest.mark.parametrize("label", [None, "https://private-model", False])
def test_composition_refuses_a_present_invalid_label(label):
    with pytest.raises(ProvenanceValidationError, match="invalid_model_label"):
        composition.selected_model({"libretranslate": label}, "libretranslate")


@pytest.mark.parametrize("label,expected,revision", [(None, composition.LegacyTranslationRuntime, 1),
                                                     ("local-v1", composition.RichTranslationRuntime, 2)])
def test_production_composition_constructs_only_the_selected_runtime(tmp_path, monkeypatch, label, expected, revision):
    from types import SimpleNamespace
    from ayvu.translate_project import TranslateObserver, TranslateProjectResult
    opened, commands, preparations = [], [], []
    project_id = document().project_id
    monkeypatch.setattr(composition, "ProjectJsonRepository", lambda _: SimpleNamespace(
        resolve_database_paths=lambda _: SimpleNamespace(database=tmp_path / "project.sqlite", backups=tmp_path / "backups"),
        resolve_artifact_root=lambda _: tmp_path / "artifacts"))
    def execute(command, **kwargs):
        commands.append(command)
        return TranslateProjectResult(project_id, failure=JobFailure(JobFailureFamily.INPUT, "fixture_gate"))
    def open_case(*args, runtime, **kwargs):
        opened.append(runtime)
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(composition, "open_translate_project", open_case)
    request = composition.ProjectTranslation(tmp_path, project_id, "a" * 64, tmp_path / "book.epub",
        TranslationOptions(language_pair=PAIR), OutputPlan(tmp_path / "out.epub"), False,
        lambda: preparations.append("provider"), lambda: preparations.append("v1"),
        model_label=label, cache_path=tmp_path / "cache.sqlite")
    result = composition.translate_in_project(request, TranslateObserver())
    assert type(opened[0]) is expected and commands[0].execution_revision == revision
    assert preparations == [] and result.result.failure.code == "fixture_gate"
    assert (commands[0].producer is None) == (label is None)
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


def runtime(tmp_path, *, options=None, prepare=None, label="local-v1"):
    return composition.RichTranslationRuntime(tmp_path / "source.epub", options or TranslationOptions(language_pair=PAIR),
        prepare or (lambda: composition.LegacyResources(None)), tmp_path / "cache.sqlite", label,
        tmp_path / "project.sqlite", tmp_path / "backups", tmp_path / "artifacts", document().project_id, clock=lambda: NOW)


@pytest.mark.parametrize("changes,message", [({"workers": 2}, "--workers 1"),
    ({"cache_only": True}, "--cache-only"), ({"require_full_cache": True}, "--require-full-cache")])
def test_each_unsupported_option_is_the_actual_gate_before_cache_and_provider(tmp_path, changes, message):
    calls = []
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR, **changes), prepare=lambda: calls.append("provider"))
    assert subject.preflight().code == "preflight_failed"
    assert message in subject.preflight_error.summary
    assert calls == [] and not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


@pytest.mark.parametrize("problem,code", [("platform", "cache_platform_unsupported"), ("foreign", "cache_foreign"),
                                         ("v1", "cache_foreign"), ("symlink", "unsafe_cache_path")])
def test_cache_is_refused_before_preparation_and_errors_are_sanitized(tmp_path, monkeypatch, problem, code):
    path = composition.v2_cache_path(tmp_path / "cache.sqlite")
    calls = []
    if problem == "platform":
        monkeypatch.setattr("ayvu.cache_schema._supported", lambda _: False)
    elif problem == "foreign":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE private(x)")
    elif problem == "v1":
        with TranslationCache(path) as cache:
            cache.set(CacheKey("private source", PAIR), "private result")
    else:
        path.symlink_to(tmp_path / "other")
    subject = runtime(tmp_path, prepare=lambda: calls.append("provider"))
    before = path.read_bytes() if path.exists() else None
    failure = subject.preflight()
    assert failure.code == code and subject.preflight_error.detail == code
    assert calls == [] and str(path) not in str(subject.preflight_error)
    if before is not None:
        assert path.read_bytes() == before


def test_prepare_cache_is_complete_before_provider_preparation_and_preserves_v1(tmp_path):
    path = tmp_path / "cache.sqlite"
    with TranslationCache(path) as cache:
        cache.set(CacheKey("private source", PAIR), "private result")
    before = path.read_bytes()
    called = []
    def prepare():
        assert classify_cache(composition.v2_cache_path(path)) is SchemaState.V2
        assert path.read_bytes() == before
        called.append(True)
        return composition.LegacyResources(None)
    assert runtime(tmp_path, prepare=prepare).preflight() is None
    assert called == [True] and path.read_bytes() == before


def test_concurrent_preparation_and_put_if_absent_at_the_production_sibling(tmp_path):
    # Keep the inode stable here so this test isolates lock contention. Initial
    # creation and refusal of substituted files are separate repository contracts.
    assert runtime(tmp_path).preflight() is None
    barrier = Barrier(2)
    def prepare(_):
        subject = runtime(tmp_path)
        barrier.wait()
        return subject, subject.preflight()
    with ThreadPoolExecutor(max_workers=2) as workers:
        prepared = list(workers.map(prepare, range(2)))
    # Read-only classification may itself overlap the other exclusive preparation.
    # Contention refuses safely; a retry after both finish must prepare the same v2.
    for subject, failure in prepared:
        if failure is not None:
            assert failure.code == "cache_busy"
            assert subject.preflight() is None
    caches = [subject._cache for subject, _ in prepared]
    item = entry()
    def put(index):
        barrier.wait()
        return caches[index].put_if_absent(item).state
    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(put, range(2)))
    assert set(states) == {PutIfAbsentState.INSERTED, PutIfAbsentState.IDEMPOTENT}


def test_existing_empty_v1_and_its_mode_are_untouched_without_hardlinks(tmp_path, monkeypatch):
    import errno
    import os
    import stat
    legacy = tmp_path / "cache.sqlite"
    legacy.touch(mode=0o640)
    legacy.chmod(0o640)
    def no_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "fixture")
    monkeypatch.setattr(os, "link", no_link)
    previous = os.umask(0o077)
    try:
        assert runtime(tmp_path).preflight() is None
    finally:
        os.umask(previous)
    assert legacy.read_bytes() == b"" and stat.S_IMODE(legacy.stat().st_mode) == 0o640
    assert stat.S_IMODE(composition.v2_cache_path(legacy).stat().st_mode) == 0o600


def test_a_held_preparation_lock_refuses_before_provider(tmp_path):
    from ayvu.cache_schema import _application_lock
    first = runtime(tmp_path)
    assert first.preflight() is None
    calls = []
    second = runtime(tmp_path, prepare=lambda: calls.append("provider"))
    with _application_lock(composition.v2_cache_path(tmp_path / "cache.sqlite"), exclusive=True):
        failure = second.preflight()
    assert failure.code == "cache_busy" and calls == []


def test_rich_preparation_keeps_the_book_policy_refusal_code(tmp_path):
    def prepare():
        raise BookRefused("encrypted_content")
    subject = runtime(tmp_path, prepare=prepare)
    failure = subject.preflight()
    assert failure.family is JobFailureFamily.INPUT and failure.code == "encrypted_content"
    assert isinstance(subject.preflight_error, BookRefused)


def prepared_runtime(tmp_path, monkeypatch, *, label="local-v1", session=None, glossary=None):
    """Unit harness: real document pipeline/cache/HTTP adapter, in-memory artifact and provenance ports."""
    env = Env()
    translator = LibreTranslateTranslator(retries=0)
    translator.session = session or Session()
    subject = runtime(tmp_path, label=label, prepare=lambda: composition.LegacyResources(translator, glossary))
    assert subject.preflight() is None
    monkeypatch.setattr(composition, "open_artifact_service", lambda *a, **kw: env.artifacts)
    monkeypatch.setattr(composition, "open_provenance_repository", lambda *a, **kw: env.provenance)
    context = RuntimeContext(env.job, CancellationSource((*composition.DOCUMENT_SAFE_POINTS, *LIBRETRANSLATE_SAFE_POINTS)), env.events)
    return subject, context, env, translator.session


def test_rich_resources_use_real_document_translator_and_cache_label_miss_restore_and_glossary(tmp_path, monkeypatch, caplog):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Hello": "Olá"}')
    source = document("Hello", "Good night")
    legacy_path = tmp_path / "cache.sqlite"
    with TranslationCache(legacy_path) as legacy:
        legacy.set(CacheKey("Hello", PAIR), "V1_ONLY")
    legacy_bytes = legacy_path.read_bytes()
    calls = []
    for label, glossary, expected_calls in [("model-a", load_glossary(glossary_path), 2), ("model-a", None, 0),
                                            ("model-b", None, 2), ("model-a", None, 0)]:
        subject, context, env, session = prepared_runtime(tmp_path, monkeypatch, label=label, glossary=glossary)
        with caplog.at_level(logging.DEBUG), subject.resources(context) as resources:
            assert isinstance(resources.executor._translator, DocumentTranslator)
            resources.prepare_document(source)
            results = [resources.executor.execute(unit) for unit in source.units]
        assert len(session.posts) == expected_calls
        assert env.artifacts.reuse_calls == []
        assert ("Olá" in results[0].translated) == (glossary is not None)
        assert all(call.producer.model == "declared:" + label for call in env.provenance.calls)
        surfaces = repr(subject) + repr(resources) + repr(results) + caplog.text + ''.join(encode_event(e) for e in env.events.calls)
        assert label not in surfaces and subject.producer.model not in surfaces
        assert legacy_path.read_bytes() == legacy_bytes
        calls.extend(session.posts)
    assert len(calls) == 4
    with sqlite3.connect(composition.v2_cache_path(tmp_path / "cache.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (4,)
        assert all("Olá" not in row[0] for row in connection.execute("SELECT value FROM cache_entries_v2"))


@pytest.mark.parametrize("index", [0, 1, 2])
def test_first_middle_last_provider_failure_stops_and_retry_uses_cached_segments(tmp_path, monkeypatch, index):
    texts = ("first", "middle", "last")
    subject, context, env, session = prepared_runtime(tmp_path, monkeypatch, session=Session(failing=texts[index]))
    with subject.resources(context) as resources, pytest.raises(TranslationExecutionError) as raised:
        resources.prepare_document(document(*texts))
    assert raised.value.failure.code == "provider_server_error"
    assert session.posts == list(texts[:index + 1])
    assert env.artifacts.calls == []
    retried, context, _, session = prepared_runtime(tmp_path, monkeypatch)
    with retried.resources(context) as resources:
        resources.prepare_document(document(*texts))
    assert session.posts == list(texts[index:])


@pytest.mark.parametrize("point", PIPELINE_SAFE_POINTS)
def test_runtime_pipeline_cancellation_acknowledges_the_requested_point(tmp_path, monkeypatch, point):
    from test_translate_project import CancelAt
    subject, context, env, session = prepared_runtime(tmp_path, monkeypatch)
    cancellation = CancelAt((*composition.DOCUMENT_SAFE_POINTS, *LIBRETRANSLATE_SAFE_POINTS), point)
    context = replace(context, cancellation=cancellation)
    with subject.resources(context) as resources, pytest.raises(TranslationExecutionError) as raised:
        resources.prepare_document(document("Hello"))
    assert raised.value.failure.code == "translation_cancelled"
    assert cancellation.reached and cancellation.acknowledgement.safe_point == point
    assert not session.posts and not env.artifacts.calls


def test_selection_remains_in_composition():
    root = Path(__file__).resolve().parents[1] / "src" / "ayvu"
    for name in ("translate_project.py", "translation_engine.py", "kernel/values.py"):
        tree = ast.parse((root / name).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not {"cache_repository", "translation_document", "provider_libretranslate", "composition"} & ({(m or "").split(".")[0] for m in imports} | set(imports))


@pytest.mark.parametrize("metadata,alt", [(False, False), (True, True)])
def test_rich_engine_reconstructs_actual_epub_preserving_structure(tmp_path, monkeypatch, metadata, alt):
    import hashlib
    import io
    from uuid import uuid4
    from zipfile import ZipFile
    from ayvu.artifact import ArtifactRequest, mark_ready, new_artifact, record_payload
    from ayvu.format_adapter import AliasVerdict
    from ayvu.format_local import LocalFormatSource
    from ayvu.format_service import FormatPublication
    from ayvu.translation_engine import EngineRequest, TranslationEngine
    from test_cli_translate_project import _book
    from format_contract import FixedGuard

    class Publisher:
        data = b""
        def publish(self, request, chunks, **kwargs):
            self.data = b"".join(chunks)
            artifact = new_artifact(request, str(uuid4()), NOW)
            artifact = record_payload(artifact, len(self.data), "sha256:" + hashlib.sha256(self.data).hexdigest(),
                                      expected_revision=artifact.revision, now=NOW)
            return mark_ready(artifact, expected_revision=artifact.revision, now=NOW)

    source = _book(tmp_path / "source.epub", chapter_two='<p id="kept">Goodbye <em>reader</em>.</p><script>keep()</script>')
    original = source.read_bytes()
    for attempt in range(2):
        subject, context, env, transport = prepared_runtime(tmp_path, monkeypatch)
        publisher = Publisher()
        publication = FormatPublication(publisher, ArtifactRequest(env.job.request.project_id, env.job.job_id,
            "translated_output", "reconstruct", 1, "sha256:" + "e" * 64), env.job.lease.owner_token, FixedGuard(AliasVerdict.DISTINCT))
        with subject.resources(context) as resources:
            result = TranslationEngine(composition.epub_format_service(), resources.executor,
                prepare_document=resources.prepare_document).run(EngineRequest("epub", LocalFormatSource(source), publication,
                env.job.request.project_id, subject.facts(), translate_metadata=metadata, translate_alt_text=alt))
        assert result.failure is None and result.output_written, result.failure
        if attempt:
            assert transport.posts == [] and result.stats.from_cache > 0
        else:
            assert transport.posts
        with ZipFile(io.BytesIO(publisher.data)) as output, ZipFile(source) as input_book:
            chapter = output.read("OEBPS/c2.xhtml")
            assert b'kept' in chapter and b'<em>reader</em>' in chapter and b'keep()' in chapter
            assert b'PT:Goodbye' in chapter
            assert output.namelist() == input_book.namelist()
            assert output.read("META-INF/container.xml") == input_book.read("META-INF/container.xml")
            if not metadata:
                assert output.read("OEBPS/content.opf") == input_book.read("OEBPS/content.opf")
        assert source.read_bytes() == original


@pytest.mark.parametrize("parent_version,new_version", [(1, 2), (2, 1)])
def test_generic_job_port_refuses_runtime_revision_mismatch_before_preflight(tmp_path, monkeypatch, parent_version, new_version):
    from types import SimpleNamespace
    from ayvu.job import JobRequest, new_job
    from ayvu.translate_project import ContinuationTicket, TranslateProjectCommand
    from test_translate_batch_project_contract import _use_case, PROJECT, JOB, NOW, DIGEST, _options
    use_case, jobs = _use_case()
    parent = new_job(JobRequest(PROJECT, "translate_project", parent_version, "sha256:" + "a" * 64), JOB, NOW)
    jobs.get = lambda _: SimpleNamespace(job=parent)
    jobs.get_continuation = lambda _: None
    called = []
    use_case._ports.runtime.preflight = lambda: called.append("preflight")
    command = TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / "out.epub"), execution_revision=new_version)
    result = use_case.execute(command, continuation=ContinuationTicket(JOB, parent.revision))
    assert result.failure.code == "runtime_mismatch" and result.job_id is None
    assert jobs.job is None and called == []


def test_generic_execution_identity_is_in_fingerprint_job_record_provenance_and_not_events(tmp_path, monkeypatch):
    from ayvu.job_events import JobEventSequence
    from ayvu.translate_project import TranslateProjectCommand, resolve_plan as job_plan
    from ayvu.translation_engine import EngineResult
    from test_translate_batch_project_contract import _use_case, PROJECT, DIGEST, _options
    use_case, jobs = _use_case()
    producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model("private-model-canary-252"))
    legacy = TranslateProjectCommand(PROJECT, DIGEST, _options(), OutputPlan(tmp_path / "out.epub"))
    rich = replace(legacy, execution_revision=2, producer=producer, additional_output_roles=("raw_results",))
    project = use_case._ports.projects.load(PROJECT)
    assert job_plan(legacy, project).input_fingerprint != job_plan(rich, project).input_fingerprint
    changed = replace(rich, producer=replace(producer, model=declared_model("other")))
    assert job_plan(changed, project).input_fingerprint != job_plan(rich, project).input_fingerprint
    provenance = []
    use_case._ports.provenance.append = provenance.append
    monkeypatch.setattr("ayvu.translate_project.TranslationEngine.run", lambda *args: EngineResult(
        failure=JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_server_error", True)))
    result = use_case.execute(rich)
    assert result.state.value == "failed" and result.failure.code == "provider_server_error"
    assert jobs.job.request.command_version == 2 and jobs.job.request.output_roles == ("translated_output", "raw_results")
    assert [record.operation for record in provenance] == ["execution_identity", "translate_project"]
    assert all(record.producer == producer for record in provenance)
    stream = JobEventSequence(result.job_id)
    for event in result.events:
        stream.accept(event)
    assert stream.outcome == result.outcome
    surfaces = repr(rich) + repr(result) + repr(jobs.job) + repr(provenance) + ''.join(encode_event(e) for e in result.events)
    assert "private-model-canary-252" not in surfaces and job_plan(rich, project).input_fingerprint not in surfaces


def test_document_preparation_failure_stops_the_engine_before_later_documents_and_publication(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ayvu.translation_engine import EngineRequest, TranslationEngine
    docs = (document("done", key="first.txt"), document("failing", key="middle.txt"), document("later", key="last.txt"))
    subject, context, env, transport = prepared_runtime(tmp_path, monkeypatch, session=Session(failing="failing"))
    publications = []
    formats = SimpleNamespace(extract=lambda *a, **kw: SimpleNamespace(documents=docs, failure=None),
                              reconstruct=lambda *a, **kw: publications.append(True))
    with subject.resources(context) as resources, pytest.raises(TranslationExecutionError) as error:
        TranslationEngine(formats, resources.executor, prepare_document=resources.prepare_document).run(
            EngineRequest("epub", object(), object(), env.job.request.project_id))
    assert error.value.failure.code == "provider_server_error"
    assert transport.posts == ["done", "failing"] and publications == []
    assert len(env.artifacts.calls) == 1  # The completed first document has raw results, never a partial EPUB.


@pytest.mark.parametrize("ownership", ["renewed", "lost"])
def test_document_uses_the_renewed_lease_but_keeps_the_original_owner(tmp_path, monkeypatch, ownership):
    from datetime import timedelta
    subject, context, env, transport = prepared_runtime(tmp_path, monkeypatch)
    subject._clock = lambda: NOW + timedelta(minutes=16)
    assert context.job.lease.expired(subject._clock())
    fresh = replace(env.job, lease=replace(env.job.lease, expires_at=NOW + timedelta(minutes=30),
                                          owner_token=env.job.lease.owner_token if ownership == "renewed" else "b" * 64))
    reads = []
    def current_job():
        reads.append(True)
        return fresh
    context = replace(context, current_job=current_job)
    with subject.resources(context) as resources:
        if ownership == "renewed":
            resources.prepare_document(document("Hello"))
            assert transport.posts == ["Hello"]
        else:
            with pytest.raises(TranslationExecutionError, match="lease_not_held"):
                resources.prepare_document(document("Hello"))
            assert transport.posts == []
    assert reads == [True]


@pytest.mark.parametrize("revision", [1, 2])
def test_checkpoint_stores_only_the_execution_revision_and_keeps_legacy_payload_unchanged(tmp_path, revision):
    from test_job_recovery_integration import Scene
    from ayvu.v1.resume import TranslationResumeState
    scene = Scene(tmp_path)
    path = scene.save_state(execution_revision=revision)
    data = json.loads(path.read_text())
    assert ("execution_revision" in data) == (revision != 1)
    assert TranslationResumeState.from_dict(data).execution_revision == revision
    assert not {"translator_models", "model", "model_label", "producer"} & set(data)


@pytest.mark.parametrize("label", [None, "private-checkpoint-label"])
def test_production_preparation_records_the_runtime_revision_without_copying_the_label(tmp_path, label):
    from types import SimpleNamespace
    from ayvu.composition.cli import prepare_translation
    from test_cli_translate_project import _book
    saved, preflights = [], []
    def preflight(**kwargs):
        preflights.append(kwargs)
        return SimpleNamespace(route=None)
    prepare_translation(epub_path=_book(tmp_path / "source.epub"), options=TranslationOptions(language_pair=PAIR),
        cache_path=tmp_path / "cache.sqlite", glossary_path=None, translator_name="libretranslate", url="http://localhost:5000",
        timeout=30, retries=0, requests_per_second=None, retry_backoff=0.5, retry_backoff_max=8,
        output_path=tmp_path / "out.epub", overwrite=False, processing_dir=tmp_path / "processing", profile_name=None,
        preflight=preflight, save_checkpoint=lambda **values: saved.append(values), on_route=lambda _: None, on_ready=lambda: None,
        translator_models={} if label is None else {"libretranslate": label}, project_backed=True)
    assert saved[0].get("execution_revision", 1) == (1 if label is None else 2)
    assert ("cache_check" in preflights[0]) == (label is not None)
    if label is not None:
        assert label not in repr(saved)


@pytest.mark.parametrize("evidence_state", ["identified", "missing", "wrong_model"])
def test_rich_checkpoint_associates_with_the_recorded_job_without_current_configuration(tmp_path, monkeypatch, evidence_state):
    from types import SimpleNamespace
    from uuid import UUID
    from ayvu.job import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
    from ayvu.job_service import JobSnapshot
    from ayvu.job_recovery import CompatibilityStatus
    from ayvu.translate_project import TranslateProjectCommand, resolve_plan as job_plan
    from test_job_recovery_integration import Scene
    scene = Scene(tmp_path)
    project = scene.project()
    path = scene.save_state(execution_revision=2)
    checkpoint_bytes = path.read_bytes()
    producer = replace(LIBRETRANSLATE_PRODUCER, model=declared_model("original-model"))
    command = TranslateProjectCommand(project.project_id, project.source_sha256, scene.options, OutputPlan(scene.output),
        execution_revision=2, producer=producer, additional_output_roles=("raw_results",))
    request = JobRequest(project.project_id, "translate_project", 2, job_plan(command, project).input_fingerprint,
                         ("translated_output", "raw_results"))
    job = new_job(request, str(UUID(int=242)), NOW)
    job = transition_job(job, JobState.PREFLIGHT, expected_revision=job.revision, now=NOW)
    job = transition_job(job, JobState.FAILED, expected_revision=job.revision, now=NOW, summary=JobTerminalSummary("provider_server_error"))
    recovery = scene.recovery()
    monkeypatch.setattr(recovery.evidence, "jobs", lambda _: (JobSnapshot(job, False),))
    recorded_producer = producer if evidence_state != "wrong_model" else replace(producer, model=declared_model("wrong-model"))
    nodes = () if evidence_state == "missing" else (SimpleNamespace(record=SimpleNamespace(request=SimpleNamespace(producer=recorded_producer))),)
    monkeypatch.setattr("ayvu.job_recovery_store.SqliteProvenanceRepository", lambda *a, **kw: SimpleNamespace(
        list_by_job=lambda *a, **kw: SimpleNamespace(nodes=nodes)))
    entries = recovery.list_interrupted.execute().entries
    if evidence_state == "identified":
        assert len(entries) == 1 and entries[0].compatibility is CompatibilityStatus.DURABLE
        assert entries[0].job_id == job.job_id and entries[0].resumable
    else:
        assert entries and not any(entry.resumable for entry in entries)
        assert not any(entry.compatibility is CompatibilityStatus.LEGACY_ONLY for entry in entries)
    assert path.read_bytes() == checkpoint_bytes



def test_translation_memory_is_supported_by_real_rich_preflight_without_v1(tmp_path):
    calls = []
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR, translation_memory=TranslationMemoryOptions()),
        prepare=lambda: (calls.append('prepare'), composition.LegacyResources(None))[1])
    assert subject.preflight() is None and calls == ['prepare']
    assert composition.v2_cache_path(tmp_path / 'cache.sqlite').exists()
    assert not (tmp_path / 'cache.sqlite').exists()


def test_strict_tm_policy_is_the_actual_preflight_gate_before_cache_or_provider(tmp_path):
    calls = []
    subject = runtime(tmp_path, options=TranslationOptions(language_pair=PAIR,
        translation_memory=TranslationMemoryOptions(conservative_auto_apply=True, apply_threshold=0.95)),
        prepare=lambda: calls.append('provider'))
    assert subject.preflight().code == 'preflight_failed'
    assert '0.98' in subject.preflight_error.next_step
    assert calls == [] and not composition.v2_cache_path(tmp_path / 'cache.sqlite').exists()
