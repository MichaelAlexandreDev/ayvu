from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
import pytest
from ayvu.jobs.model import JobState
from ayvu.translation.context.model import ContextCandidate, ContextLimits, TranslationContextBuilder
from ayvu.translation.document_contracts import TranslationExecutionError, UnitSource
from ayvu.translation.plan import resolve_plan
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from tests.support.translation_document_support import document, cue, PlanCache, NOW, PROJECT
from tests.support.translation_context_support import entry, Source
from ayvu.kernel import JobWarning, TranslationOptions, LanguagePair, OutputPlan, JobFailure, JobFailureFamily
from tests.support.context_pipeline_support import Gate, scene


def test_context_warning_reaches_cli_presentation_with_isolated_user_paths(tmp_path, monkeypatch):
    from datetime import datetime, UTC
    from ayvu.jobs.events import JobCorrelation, JobEvent
    from tests.support.cli_translate_project_support import _canned, _report, _result
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.chdir(tmp_path)
    event = JobEvent(str(UUID(int=1)), str(UUID(int=2)), 1, datetime.now(UTC),
        JobCorrelation(str(UUID(int=3))), JobWarning('context_unsupported'))
    result = _result(JobState.VALIDATED, 'validated', events=(event,))
    invocation = _canned(tmp_path, monkeypatch, result, _report)
    assert invocation.exit_code == 0, invocation.output
    assert 'Aviso da tradução:' in invocation.output and 'context_unsupported' in invocation.output


def test_real_rich_runtime_resources_pass_context_to_document_translator(tmp_path, monkeypatch):
    import ayvu.composition.builders.translate_project as composition
    from ayvu.translation.project_contracts import RuntimeContext
    s = scene()
    runtime = composition.RichTranslationRuntime(tmp_path / 'book.epub', TranslationOptions(language_pair=LanguagePair('en', 'pt')),
        lambda: None, tmp_path / 'cache.sqlite', 'model-v1', tmp_path / 'project.sqlite', tmp_path / 'backups',
        tmp_path / 'artifacts', PROJECT, clock=lambda: NOW, translation_context=s.execution)
    base = LibreTranslateTranslator('http://127.0.0.1:5000')
    runtime._prepared = composition.LegacyResources(base)
    runtime._cache = s.cache
    monkeypatch.setattr(composition, 'open_artifact_service', lambda *a, **kw: s.artifacts)
    monkeypatch.setattr(composition, 'open_provenance_repository', lambda *a, **kw: s.provenance)
    captured = []
    real = composition.DocumentTranslator
    def capture(*a, **kw):
        captured.append(kw['translation_context'])
        return real(*a, **kw)
    monkeypatch.setattr(composition, 'DocumentTranslator', capture)
    context = RuntimeContext(s.job, None, s.events)
    with runtime.resources(context) as resources:
        assert resources.prepare_document is not None
    assert captured == [s.execution]


def test_rich_runtime_denial_precedes_cache_preparation_and_provider_resolution(tmp_path):
    import ayvu.composition.builders.translate_project as composition
    s = scene(gate=Gate(lambda d: replace(d, allowed=False)))
    resolved = []
    runtime = composition.RichTranslationRuntime(tmp_path / 'book.epub', TranslationOptions(language_pair=LanguagePair('en', 'pt')),
        lambda: resolved.append('provider'), tmp_path / 'cache.sqlite', 'model-v1', tmp_path / 'project.sqlite',
        tmp_path / 'backups', tmp_path / 'artifacts', PROJECT, clock=lambda: NOW, translation_context=s.execution)
    assert runtime.preflight().code == 'preflight_failed'
    assert runtime.preflight_error.code == 'context_egress_denied'
    assert not resolved and not composition.v2_cache_path(tmp_path / 'cache.sqlite').exists()


@pytest.mark.parametrize('change', ['manifest', 'capability'])
def test_prepared_context_cannot_execute_against_a_replaced_manifest_or_capability(change):
    s = scene()
    r = s.r
    if change == 'manifest':
        r = replace(r, snapshot=replace(r.snapshot, entries=r.snapshot.entries + (entry(index=2, required=True),)))
    else:
        r = replace(r, capabilities=replace(r.capabilities, expires_at=NOW + timedelta(minutes=4)))
    s.execution.requests = (r,)
    with pytest.raises(TranslationExecutionError) as error:
        s.translator.translate(s.d, s.context)
    assert error.value.failure.code == 'context_integrity'
    assert not s.cache.calls and not s.gate.calls and not s.provider.requests
    assert not s.artifacts.published and not s.provenance.records


def test_all_unit_bindings_are_checked_before_a_pinned_cache_port():
    from ayvu.knowledge.cache.cache_only import CacheOnlyMode, CacheOnlyRequest, PinnedExecutionIdentityReference
    s = scene()
    # O segundo elemento não está autorizado; nem a resolução do pin pode ocorrer.
    d = document(cue('a', 'Hello world', 1), cue('b', 'other unit', 2))
    from ayvu.providers.ports import UnitReference
    snapshot = replace(s.r.snapshot, unit=UnitReference(d.document_id, d.revision_id, d.units[0].unit_id), current=d.units[0])
    s.execution.requests = (replace(s.r, snapshot=snapshot, authorization=replace(s.r.authorization, unit=snapshot.unit)),)
    s.execution.prepare(NOW)
    assert s.execution.for_unit(d, d.units[0], NOW, s.p) is not None
    class Cache(PlanCache):
        def resolve_pinned(self, *a):
            pytest.fail('pin resolved before validating all units')
    s.translator._exact_cache = Cache()
    pin = PinnedExecutionIdentityReference('execution:' + 'a' * 64)
    with pytest.raises(TranslationExecutionError) as error:
        s.translator.translate(d, s.context, cache_only=CacheOnlyRequest(CacheOnlyMode.PINNED, pin))
    assert error.value.failure.code == 'context_authorization'
    assert not s.gate.calls and not s.provider.requests


def test_pipeline_cache_ignores_unused_context_and_inert_profile_changes():
    def prepare(s, unused, style):
        from ayvu.projects.profiles.model import ProjectTranslationProfile
        r = s.r
        e = entry(unused, index=2)
        profile = replace(r.snapshot.profile, profile_value=ProjectTranslationProfile(style=style))
        snapshot = replace(r.snapshot, entries=(s.e, e), profile=profile)
        r = replace(r, snapshot=snapshot, limits=ContextLimits(entries=1),
            authorization=replace(r.authorization, profile_revision=profile.reference.value))
        s.execution.requests = (r,)
        s.execution.builder = TranslationContextBuilder(Source((ContextCandidate(e, unused), ContextCandidate(s.e, 'contexto'))))
        s.execution.prepare(NOW)
    a = scene()
    prepare(a, 'unused one', 'literary')
    assert a.translator.translate(a.d, a.context).units[0].source is UnitSource.PROVIDER
    b = scene(cache=a.cache)
    prepare(b, 'unused two', 'technical')
    assert b.translator.translate(b.d, b.context).units[0].source is UnitSource.EXACT_CACHE
    assert not b.provider.requests and not b.gate.calls


def test_composition_binds_context_before_repositories_and_puts_effect_in_job_plan(tmp_path, monkeypatch):
    import ayvu.composition.builders.translate_project as composition
    from ayvu.translation.project_contracts import TranslateObserver, TranslateProjectResult
    from tests.support.translation_context_support import OPERATION
    s = scene()
    # Relógio da fronteira real, sem rede nem projeto SQLite.
    monkeypatch.setattr(composition, 'datetime', SimpleNamespace(now=lambda _: NOW))
    opened, commands, resolutions = [], [], []
    monkeypatch.setattr(composition, 'ProjectJsonRepository', lambda _: SimpleNamespace(
        resolve_database_paths=lambda _: SimpleNamespace(database=tmp_path / 'project.sqlite', backups=tmp_path / 'backups'),
        resolve_artifact_root=lambda _: tmp_path / 'artifacts'))
    def open_case(*args, runtime, **kwargs):
        opened.append(runtime)
        def execute(command, **kw):
            commands.append(command)
            return TranslateProjectResult(PROJECT, failure=JobFailure(JobFailureFamily.INPUT, 'fixture_gate'))
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(composition, 'open_translate_project', open_case)
    translation = composition.ProjectTranslation(tmp_path, PROJECT, 'a' * 64, tmp_path / 'book.epub',
        TranslationOptions(language_pair=LanguagePair('en', 'pt')), OutputPlan(tmp_path / 'out.epub'), False,
        lambda: resolutions.append('provider'), lambda: resolutions.append('cache'),
        operation_id=OPERATION, model_label='model-v1', cache_path=tmp_path / 'cache.sqlite', translation_context=s.execution)
    composition.translate_in_project(translation, TranslateObserver())
    assert len(opened) == 1 and type(opened[0]) is composition.RichTranslationRuntime
    assert commands[0].context_revision == s.execution.job_revision
    assert commands[0].additional_output_roles == ('raw_results',)
    assert not resolutions
    assert len(s.gate.calls) == 1
    # A recusa é o gate de contexto; abrir o projeto/resolver provider seria erro.
    monkeypatch.setattr(composition, 'ProjectJsonRepository', lambda _: pytest.fail('repository reached after denial'))
    s.gate.change = lambda d: replace(d, allowed=False)
    with pytest.raises(composition.ProjectTranslationUnavailable, match='context_egress_denied'):
        composition.translate_in_project(translation, TranslateObserver())
    assert not resolutions


def test_context_job_plan_changes_only_when_declared(tmp_path):
    from ayvu.translation.project_contracts import TranslateProjectCommand, resolve_execution_plan as resolve_plan
    # Reutiliza o manifesto mínimo já validado pelo contrato de projeto.
    from tests.support.project_support import manifest
    project = manifest()
    command = TranslateProjectCommand(project.project_id, 'a' * 64, TranslationOptions(language_pair=LanguagePair('en', 'pt')),
        OutputPlan(tmp_path / 'out.epub'))
    base = resolve_plan(command, project).input_fingerprint
    assert resolve_plan(replace(command, context_revision=None), project).input_fingerprint == base
    a = replace(command, context_revision='sha256:' + 'a' * 64)
    b = replace(command, context_revision='sha256:' + 'b' * 64)
    assert len({base, resolve_plan(a, project).input_fingerprint, resolve_plan(b, project).input_fingerprint}) == 3


@pytest.mark.parametrize('binding', [False, True])
def test_composition_cancellation_stops_before_candidate_loading_and_project_ports(tmp_path, monkeypatch, binding):
    from contextlib import contextmanager
    import ayvu.composition.builders.translate_project as composition
    from ayvu.jobs.execution import CancellationSource
    from ayvu.translation.project_contracts import TranslateObserver
    from tests.support.translation_context_support import OPERATION
    s = scene()
    s.source.calls.clear()
    monkeypatch.setattr(composition, 'datetime', SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr(composition, 'ProjectJsonRepository', lambda _: pytest.fail('repository reached after cancellation'))
    class Binding:
        @contextmanager
        def bind(self, source):
            source.request('user_cancelled', NOW)
            yield source
    source = CancellationSource(('pipeline_between_stages',))
    source.request('user_cancelled', NOW)
    translation = composition.ProjectTranslation(tmp_path, PROJECT, 'a' * 64, tmp_path / 'book.epub',
        TranslationOptions(language_pair=LanguagePair('en', 'pt')), OutputPlan(tmp_path / 'out.epub'), False,
        lambda: pytest.fail('provider resolved'), lambda: pytest.fail('cache opened'),
        operation_id=OPERATION, model_label='model-v1', cache_path=tmp_path / 'cache.sqlite', translation_context=s.execution)
    with pytest.raises(composition.ProjectTranslationUnavailable, match='context_cancelled'):
        composition.translate_in_project(translation, TranslateObserver(), cancellation=Binding() if binding else source)
    assert not s.source.calls and not s.execution._prepared
