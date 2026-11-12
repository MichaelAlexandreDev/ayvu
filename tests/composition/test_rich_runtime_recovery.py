from __future__ import annotations
import json
from dataclasses import replace
import pytest
import ayvu.composition.builders.translate_project as composition
from ayvu.provenance.model import declared_model
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
from tests.support.cache_repository_v2_support import entry
from tests.support.translation_cache_v2_support import NOW
from ayvu.kernel import TranslationMemoryOptions, TranslationOptions, OutputPlan
from tests.support.rich_runtime_support import PAIR, runtime


@pytest.mark.parametrize("revision", [1, 2])
def test_checkpoint_stores_only_the_execution_revision_and_keeps_legacy_payload_unchanged(tmp_path, revision):
    from tests.support.job_recovery_integration_support import Scene
    from ayvu.recovery.checkpoint import TranslationResumeState
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
    from tests.support.cli_translate_project_support import _book
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
    from ayvu.jobs.model import JobRequest, JobState, JobTerminalSummary, new_job, transition_job
    from ayvu.jobs.service import JobSnapshot
    from ayvu.recovery.service import CompatibilityStatus
    from ayvu.translation.project_contracts import TranslateProjectCommand, resolve_execution_plan as job_plan
    from tests.support.job_recovery_integration_support import Scene
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
    monkeypatch.setattr("ayvu.recovery.store.SqliteProvenanceRepository", lambda *a, **kw: SimpleNamespace(
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
