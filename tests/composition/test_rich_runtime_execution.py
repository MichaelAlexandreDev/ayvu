from __future__ import annotations
import logging
import sqlite3
from dataclasses import replace
import pytest
import ayvu.composition.builders.translate_project as composition
from tests.support.legacy_cache_file import CacheKey, LegacyCacheFile
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.jobs.events import encode_event
from ayvu.provenance.model import declared_model
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LIBRETRANSLATE_SAFE_POINTS
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import PIPELINE_SAFE_POINTS, TranslationExecutionError
from tests.support.cli_translate_project_support import Session
from tests.support.translation_cache_v2_support import NOW, document
from ayvu.kernel import OutputPlan, JobFailure, JobFailureFamily
from tests.support.rich_runtime_support import PAIR, prepared_runtime


def test_rich_resources_use_real_document_translator_and_cache_label_miss_restore_and_glossary(tmp_path, monkeypatch, caplog):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Hello": "Olá"}')
    source = document("Hello", "Good night")
    legacy_path = tmp_path / "cache.sqlite"
    with LegacyCacheFile(legacy_path) as legacy:
        legacy.set(CacheKey("Hello", PAIR), "V1_ONLY")
    legacy_bytes = legacy_path.read_bytes()
    calls = []
    for label, glossary, expected_calls in [("model-a", load_glossary(glossary_path), 2), ("model-a", None, 0),
                                            ("model-b", None, 2), ("model-a", None, 0)]:
        subject, context, env, session = prepared_runtime(tmp_path, monkeypatch, label=label, glossary=glossary)
        with caplog.at_level(logging.DEBUG), subject.resources(context) as resources:
            results = resources.documents.execute(source).executions
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
        resources.documents.execute(document(*texts))
    assert raised.value.failure.code == "provider_server_error"
    assert session.posts == list(texts[:index + 1])
    assert env.artifacts.calls == []
    retried, context, _, session = prepared_runtime(tmp_path, monkeypatch)
    with retried.resources(context) as resources:
        resources.documents.execute(document(*texts))
    assert session.posts == list(texts[index:])


@pytest.mark.parametrize("point", PIPELINE_SAFE_POINTS)
def test_runtime_pipeline_cancellation_acknowledges_the_requested_point(tmp_path, monkeypatch, point):
    from tests.support.translate_project_support import CancelAt
    subject, context, env, session = prepared_runtime(tmp_path, monkeypatch)
    cancellation = CancelAt((*composition.DOCUMENT_SAFE_POINTS, *LIBRETRANSLATE_SAFE_POINTS), point)
    context = replace(context, cancellation=cancellation)
    with subject.resources(context) as resources, pytest.raises(TranslationExecutionError) as raised:
        resources.documents.execute(document("Hello"))
    assert raised.value.failure.code == "translation_cancelled"
    assert cancellation.reached and cancellation.acknowledgement.safe_point == point
    assert not session.posts and not env.artifacts.calls


@pytest.mark.parametrize("metadata,alt", [(False, False), (True, True)])
def test_rich_engine_reconstructs_actual_epub_preserving_structure(tmp_path, monkeypatch, metadata, alt):
    import hashlib
    import io
    from uuid import uuid4
    from zipfile import ZipFile
    from ayvu.artifacts.model import ArtifactRequest, mark_ready, new_artifact, record_payload
    from ayvu.formats.contracts import AliasVerdict
    from ayvu.formats.local import LocalFormatSource
    from ayvu.formats.service import FormatPublication
    from ayvu.translation.engine import EngineRequest, TranslationEngine
    from tests.support.cli_translate_project_support import _book
    from tests.support.format_contract_env import FixedGuard

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
                documents=resources.documents).run(EngineRequest("epub", LocalFormatSource(source), publication,
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
    from ayvu.jobs.model import JobRequest, new_job
    from ayvu.translation.project_contracts import ContinuationTicket, TranslateProjectCommand
    from tests.support.translate_batch_project_contract_support import _use_case, PROJECT, JOB, NOW, DIGEST, _options
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
    from ayvu.jobs.events import JobEventSequence
    from ayvu.translation.project_contracts import TranslateProjectCommand, resolve_execution_plan as job_plan
    from ayvu.translation.engine import EngineResult
    from tests.support.translate_batch_project_contract_support import _use_case, PROJECT, DIGEST, _options
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
    monkeypatch.setattr("ayvu.translation.engine.TranslationEngine.run", lambda *args: EngineResult(
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
    from ayvu.translation.engine import EngineRequest, TranslationEngine
    docs = (document("done", key="first.txt"), document("failing", key="middle.txt"), document("later", key="last.txt"))
    subject, context, env, transport = prepared_runtime(tmp_path, monkeypatch, session=Session(failing="failing"))
    publications = []
    formats = SimpleNamespace(extract=lambda *a, **kw: SimpleNamespace(documents=docs, failure=None),
                              reconstruct=lambda *a, **kw: publications.append(True))
    with subject.resources(context) as resources, pytest.raises(TranslationExecutionError) as error:
        TranslationEngine(formats, resources.executor, documents=resources.documents).run(
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
            resources.documents.execute(document("Hello"))
            assert transport.posts == ["Hello"]
        else:
            with pytest.raises(TranslationExecutionError, match="lease_not_held"):
                resources.documents.execute(document("Hello"))
            assert transport.posts == []
    assert reads == [True]
