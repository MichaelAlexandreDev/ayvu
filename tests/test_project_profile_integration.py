"""Local adapters, real v1 input and project CAS. No user directories or network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Barrier

import pytest

from ayvu.glossary_catalog import Entry, GlossaryReference
from ayvu.glossary_catalog_store import ManagedGlossaryRepository
from ayvu.preferences_store import JsonPreferencesRepository
from ayvu.profile_glossary_store import LegacyGlossaryResolver
from ayvu.project import ProjectConflictError
from ayvu.project_profile_composition import project_profile_service
from ayvu.project_profile_service import ProfileOutcome, ProjectProfileService
from ayvu.project_profile_store import JsonProjectProfileRepository, STATE_KEY
from ayvu.project_profiles import (
    ExecutionStage, GlossaryStage, PlanFacts, ProfileError, ProfileOverrides, ProfileReference,
    ProfileRevision, ProjectReference, ProjectTranslationProfile, ProviderRequestProjection,
)
from ayvu.project_store import ProjectJsonRepository, build_project_request


def plan(target='fr', glossary=True):
    return PlanFacts(ExecutionStage.TRANSLATE, GlossaryStage.POST_CACHE if glossary else GlossaryStage.NONE,
                     ProviderRequestProjection(target, ProfileRevision('a' * 64)))


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'original.epub'
    source.write_bytes(b'original-canary-never-modified')
    processing = tmp_path / 'processing'
    projects = ProjectJsonRepository(processing)
    manifest = projects.create(build_project_request(source, source='en', target='pt'))
    root = tmp_path / 'managed'
    catalog = ManagedGlossaryRepository(root)
    created = catalog.create(GlossaryReference('managed.json'), (Entry('book-canary', 'translate', 'livro'),))
    assert created.failure is None
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'version': 1, 'profiles': {'name-canary': {
        'target_language': 'fr', 'glossary': 'managed.json', 'style': 'literary'}}}))
    prefs = JsonPreferencesRepository(config)
    service = project_profile_service(processing, root)
    project = ProjectReference(manifest.project_id)
    destination = ProfileReference.new()
    return source, projects, root, catalog, config, prefs, service, project, destination


def migrate(local, expected=1, cancel=None):
    _, _, _, _, _, prefs, service, project, destination = local
    return service.migrate(prefs, 'name-canary', project, expected, destination, cancel)


def set_glossary(local, value):
    config = local[4]
    data = json.loads(config.read_text())
    data['profiles']['name-canary']['glossary'] = value
    config.write_text(json.dumps(data))


def test_production_preferences_exact_binding_atomic_update_and_restart(local):
    source, projects, root, catalog, config, prefs, service, project, destination = local
    before_source, before_config = source.read_bytes(), config.read_bytes()
    original = projects.load(project.value)
    glossary = catalog.read(GlossaryReference('managed.json')).value
    migrated = migrate(local)
    assert migrated.outcome is ProfileOutcome.MIGRATED
    profile = migrated.state.profile(destination)
    assert profile.glossary.reference == glossary.reference
    assert profile.glossary.revision.value == glossary.revision
    manifest = projects.load(project.value)
    assert manifest.revision == original.revision + 1
    assert manifest.request() == original.request()
    assert STATE_KEY in dict(manifest.metadata)
    assert 'name-canary' not in json.dumps(migrated.state.to_mapping())
    assert str(root) not in json.dumps(migrated.state.to_mapping())
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    assert resolved.outcome is ProfileOutcome.RESOLVED
    assert resolved.snapshot.project_revision == 2
    restarted = project_profile_service(projects.root.parent, root)
    loaded = restarted.repository.read(project)
    assert loaded.snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert loaded.raw_projection(resolved.snapshot.raw_semantic_contribution()['snapshot_reference']) == resolved.snapshot.effects.raw_projection()
    assert restarted.resolve(project, 2, destination, profile.revision, plan()).snapshot == resolved.snapshot
    assert migrate(local).outcome is ProfileOutcome.ALREADY_MIGRATED
    updated = restarted.write(project, 3, destination, replace(profile, style='technical'), profile.revision)
    assert updated.outcome is ProfileOutcome.UPDATED
    assert updated.state.snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert migrate(local).outcome is ProfileOutcome.ALREADY_MIGRATED
    assert restarted.repository.read(project).profile(destination).style == 'technical'
    assert source.read_bytes() == before_source and config.read_bytes() == before_config
    storage = projects._manifest_path(project.value).parent
    assert not list(storage.glob('*.sqlite*'))
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (storage / 'profiles').glob('*.json'))
    assert (storage / 'profiles').stat().st_mode & 0o777 == 0o700


def test_legacy_absolute_path_inside_managed_catalog_is_compatible(local):
    set_glossary(local, str(local[2] / 'managed.json'))
    result = migrate(local)
    assert result.outcome is ProfileOutcome.MIGRATED
    assert result.state.profile(local[8]).glossary.reference == GlossaryReference('managed.json')
    assert str(local[2]) not in json.dumps(result.state.to_mapping())


@pytest.mark.parametrize('kind,reason', [
    ('missing', 'missing'), ('absolute_external', 'external'), ('relative_external', 'external'),
    ('case_alias', 'ambiguous'), ('unicode_alias', 'ambiguous'), ('symlink', 'unsafe'),
    ('parent_symlink', 'unsafe'), ('hardlink', 'unsafe'), ('directory', 'unsafe'), ('invalid', 'unsafe'),
])
def test_legacy_path_rejections_use_catalog_rules_without_partial_project_update(local, tmp_path, kind, reason):
    root, projects, project = local[2], local[1], local[7]
    selected = root / 'managed.json'
    value = 'managed.json'
    if kind == 'missing':
        value = 'missing.json'
    elif kind in ('absolute_external', 'relative_external'):
        external = tmp_path / 'outside.json'
        external.write_text('{}')
        value = str(external) if kind == 'absolute_external' else '../outside.json'
    elif kind == 'case_alias':
        (root / 'MANAGED.json').write_text('{}')
    elif kind == 'unicode_alias':
        value = 'caf\u00e9.json'
        (root / value).write_text('{}')
        (root / 'cafe\u0301.json').write_text('{}')
    elif kind == 'symlink':
        selected.unlink()
        selected.symlink_to(tmp_path / 'outside.json')
    elif kind == 'parent_symlink':
        (tmp_path / 'alias').symlink_to(root, target_is_directory=True)
        value = str(tmp_path / 'alias' / 'managed.json')
    elif kind == 'hardlink':
        os.link(selected, tmp_path / 'hardlink.json')
    elif kind == 'directory':
        selected.unlink()
        selected.mkdir()
    else:
        selected.write_text('[]')
    set_glossary(local, value)
    before = projects.load(project.value)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY
    assert result.glossary_failure == reason
    assert result.state is None and result.snapshot is None
    assert projects.load(project.value) == before
    assert local[6].repository.read(project).records == ()


def test_glossary_change_after_resolution_is_refused_by_exact_revision_guard(local, monkeypatch):
    service = local[6]
    original = service.glossaries.resolve_legacy
    def race(path, cancel=None):
        binding = original(path, cancel)
        changed = local[3].update(binding.reference, (Entry('new', 'preserve'),), binding.revision.value)
        assert changed.failure is None
        return binding
    monkeypatch.setattr(service.glossaries, 'resolve_legacy', race)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY
    assert result.glossary_failure == 'stale'
    assert local[1].load(local[7].value).revision == 1
    assert service.repository.read(local[7]).records == ()


def test_glossary_change_immediately_before_manifest_publication_is_revalidated(local, monkeypatch):
    service = local[6]
    original = service.repository._publish_manifest
    def race(path, manifest, cancel, validate):
        # An uncooperative writer bypasses #157's lock. Last-moment validation
        # still detects a changed revision before publishing the project pointer.
        (local[2] / 'managed.json').write_text('{}')
        return original(path, manifest, cancel, validate)
    monkeypatch.setattr(service.repository, '_publish_manifest', race)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and result.glossary_failure == 'stale'
    assert service.repository.read(local[7]).records == ()
    assert local[1].load(local[7].value).revision == 1


def test_stale_glossary_prevents_snapshot_publication_but_keeps_historical_snapshot(local):
    migrated = migrate(local)
    service, project, destination = local[6:]
    profile = migrated.state.profile(destination)
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    current = local[3].read(profile.glossary.reference).value
    assert local[3].update(current.reference, (Entry('new', 'preserve'),), current.revision).failure is None
    failed = service.resolve(project, 3, destination, profile.revision, plan('de'), ProfileOverrides('de'))
    assert failed.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and failed.glossary_failure == 'stale'
    assert failed.snapshot is None
    assert service.repository.read(project).snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert local[1].load(project.value).revision == 3
    retry = service.resolve(project, 2, destination, profile.revision, plan())
    assert retry.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and retry.glossary_failure == 'stale'
    unchanged = service.write(project, 3, destination, profile, profile.revision)
    assert unchanged.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and unchanged.glossary_failure == 'stale'


def test_unrelated_project_update_and_stale_manifest_conflict(local):
    service, project, destination = local[6:]
    manifest = local[1].load(project.value)
    local[1].save(replace(manifest, locations=('artifacts',)), expected_revision=1)
    assert migrate(local).outcome is ProfileOutcome.CONFLICT
    assert service.repository.read(project).records == ()
    migrated = migrate(local, expected=2)
    assert migrated.outcome is ProfileOutcome.MIGRATED
    current = local[1].load(project.value)
    local[1].save(replace(current, locations=('artifacts', 'reports')), expected_revision=3)
    state = service.repository.read(project)
    assert state.project_revision == 4 and state.profile(destination).style == 'literary'
    with pytest.raises(ProjectConflictError, match='revision_conflict'):
        local[1].save(current, expected_revision=3)


@pytest.mark.parametrize('phase', ['before', 'prepared', 'write', 'pre_sync', 'post_sync'])
def test_cancellation_and_persistence_failures_preserve_prior_bytes_and_report_no_success(local, monkeypatch, phase):
    service, project, destination = local[6:]
    projects = service.repository.projects
    manifest_path = projects._manifest_path(project.value)
    # Existing permissions must survive replacement and rollback.
    manifest_path.chmod(0o400)
    before = manifest_path.read_bytes()
    hits = []
    cancel = None
    if phase == 'before':
        def cancel():
            hits.append('before')
            return True
    elif phase == 'prepared':
        prepared = False
        original = service.repository._temporary
        def temporary(path, data, mode=0o600):
            nonlocal prepared
            value = original(path, data, mode)
            if path == manifest_path:
                prepared = True
            return value
        monkeypatch.setattr(service.repository, '_temporary', temporary)
        def cancel():
            if prepared:
                hits.append('prepared')
            return prepared
    elif phase == 'write':
        def fail(*args):
            hits.append('write')
            raise OSError('private-path-credential-canary')
        monkeypatch.setattr(service.repository, '_temporary', fail)
    else:
        original = projects._sync_directory
        count = 0
        def sync(directory):
            nonlocal count
            if directory == manifest_path.parent:
                count += 1
                if count == (1 if phase == 'pre_sync' else 2):
                    hits.append(phase)
                    raise OSError('private-path-credential-canary')
            return original(directory)
        monkeypatch.setattr(projects, '_sync_directory', sync)
    result = migrate(local, cancel=cancel)
    assert hits == [phase]
    assert result.outcome is (ProfileOutcome.CANCELLED if phase in ('before', 'prepared') else ProfileOutcome.PERSISTENCE)
    assert result.state is None and result.snapshot is None
    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_mode & 0o777 == 0o400
    assert service.repository.read(project).records == ()
    assert 'canary' not in str(result.diagnostic())


def test_concurrent_divergent_expected_revision_updates_have_one_winner(local):
    barrier = Barrier(2)
    processing, root, project, destination = local[1].root.parent, local[2], local[7], local[8]
    class RacingRepository(JsonProjectProfileRepository):
        def read(self, project):
            value = super().read(project)
            barrier.wait(timeout=10)
            return value
    def write(target):
        repository = RacingRepository(ProjectJsonRepository(processing))
        service = ProjectProfileService(repository, LegacyGlossaryResolver(ManagedGlossaryRepository(root)))
        return service.write(project, 1, destination, ProjectTranslationProfile(target))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ('de', 'es')))
    assert sorted(r.outcome.value for r in results) == ['conflict', 'created']
    state = local[6].repository.read(project)
    winner = next(r for r in results if r.outcome is ProfileOutcome.CREATED)
    assert state == winner.state and len(state.records) == 1
    assert local[1].load(project.value).revision == 2


def test_divergent_migrations_cannot_both_publish_from_one_project_revision(local):
    first = migrate(local)
    data = json.loads(local[4].read_text())
    data['profiles']['name-canary']['style'] = 'technical'
    local[4].write_text(json.dumps(data))
    second = migrate(local)
    assert first.outcome is ProfileOutcome.MIGRATED and second.outcome is ProfileOutcome.CONFLICT
    assert local[6].repository.read(local[7]) == first.state


def test_concurrent_divergent_migrations_same_destination_do_not_overwrite(local, tmp_path):
    barrier = Barrier(2)
    other_config = tmp_path / 'other-config.json'
    other = json.loads(local[4].read_text())
    other['profiles']['name-canary']['target_language'] = 'de'
    other_config.write_text(json.dumps(other))
    class RacingRepository(JsonProjectProfileRepository):
        def read(self, project):
            state = super().read(project)
            barrier.wait(timeout=10)
            return state
    def run(config):
        service = ProjectProfileService(RacingRepository(ProjectJsonRepository(local[1].root.parent)),
            LegacyGlossaryResolver(ManagedGlossaryRepository(local[2])))
        return service.migrate(JsonPreferencesRepository(config), 'name-canary', local[7], 1, local[8])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (local[4], other_config)))
    assert sorted(r.outcome.value for r in results) == ['conflict', 'migrated']
    state = local[6].repository.read(local[7])
    assert len(state.records) == len(state.migrations) == 1
    assert state == next(r.state for r in results if r.outcome is ProfileOutcome.MIGRATED)


def test_migration_and_style_remain_inert_over_instrumented_libretranslate_transport(local, monkeypatch):
    # Real requests prepares the payload; only its final send boundary is replaced.
    # Migration, snapshot publication and cache replay must not reach transport.
    import requests
    from ayvu.config import TranslationProfile
    from ayvu.cli import _resolve_target_language, _resolve_glossary_path
    from ayvu.domain import LanguagePair
    from ayvu.translation_pipeline import LegacyTextPipelineFacade, LinguisticSource
    from ayvu.translator import LibreTranslateTranslator
    from test_translation_pipeline import Cache, Glossed
    sent = []
    def send(session, request, **kwargs):
        sent.append((request.method, request.url, json.loads(request.body), kwargs['timeout']))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"translatedText":"raw-canary"}'
        response.request = request
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    translator = LibreTranslateTranslator(url='http://localhost:5000', retries=0, timeout=3)
    cache = Cache()
    facade = LegacyTextPipelineFacade(translator, cache, lambda text: Glossed(text + '-glossed'))
    old = TranslationProfile('fr', Path('managed.json'), 'literary')
    assert _resolve_target_language(None, old) == 'fr'
    explicit = local[2] / 'explicit.json'
    assert _resolve_glossary_path(explicit, old) == explicit
    assert _resolve_target_language('de', old) == 'de'
    pair = LanguagePair(source='en', target='fr')
    first = facade.run('Hello', pair)
    assert first.source is LinguisticSource.PROVIDER and first.text == 'raw-canary-glossed'
    expected = [('POST', 'http://localhost:5000/translate',
        {'q': 'Hello', 'source': 'en', 'target': 'fr', 'format': 'text'}, 3)]
    assert sent == expected
    migrated = migrate(local)
    assert migrated.outcome is ProfileOutcome.MIGRATED
    service, project, destination = local[6:]
    value = migrated.state.profile(destination)
    resolved = service.resolve(project, 2, destination, value.revision, plan())
    assert resolved.outcome is ProfileOutcome.RESOLVED
    changed = service.write(project, 3, destination, replace(value, style='technical'), value.revision)
    assert changed.outcome is ProfileOutcome.UPDATED and sent == expected
    second = facade.run('Hello', pair)
    assert second.source is LinguisticSource.EXACT_CACHE
    assert first.text == second.text and sent == expected
    assert cache.stored == {('Hello', 'en', 'fr'): 'raw-canary'}
    # A cold legacy execution still creates the same exact request, with no style field.
    cold = LegacyTextPipelineFacade(translator, Cache(), lambda text: Glossed(text + '-glossed')).run('Hello', pair)
    assert cold.text == first.text and sent == expected * 2
    translator.session.close()


def test_snapshot_failure_publishes_neither_result_nor_history(local, monkeypatch):
    migrated = migrate(local)
    service, project, destination = local[6:]
    prior = service.repository.read(project)
    def fail(*args):
        raise OSError('canary')
    monkeypatch.setattr(service.repository, '_publish_manifest', fail)
    result = service.resolve(project, 2, destination, migrated.state.profile(destination).revision, plan())
    assert result.outcome is ProfileOutcome.PERSISTENCE and result.snapshot is None
    assert service.repository.read(project) == prior


def test_repository_refuses_history_deletion_even_with_exact_project_revision(local):
    migrated = migrate(local)
    service, project, destination = local[6:]
    profile = migrated.state.profile(destination)
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    state = service.repository.read(project)
    cases = [replace(state, project_revision=4, snapshots=()),
             replace(state, project_revision=4, migrations=())]
    changed = service.write(project, 3, destination, replace(profile, style='academic'), profile.revision)
    assert changed.outcome is ProfileOutcome.UPDATED
    state = service.repository.read(project)
    cases = [replace(c, project_revision=5, records=state.records, heads=state.heads) for c in cases]
    cases.append(replace(state, project_revision=5, records=(next(r for r in state.records if r.value.style == 'academic'),),
                         snapshots=(), migrations=()))
    for candidate in cases:
        with pytest.raises(ProfileError, match='conflict'):
            service.repository.publish(candidate, 4, None, lambda: None)
        assert service.repository.read(project) == state
    assert state.snapshot(resolved.snapshot.reference) == resolved.snapshot


def test_repository_cas_refuses_an_unrelated_manifest_revision_change(local):
    migrated = migrate(local)
    service, project, _ = local[6:]
    manifest = local[1].load(project.value)
    current = local[1].save(replace(manifest, locations=('reports',)), expected_revision=2)
    candidate = replace(migrated.state, project_revision=3)
    with pytest.raises(ProfileError, match='conflict'):
        service.repository.publish(candidate, 2, None, lambda: None)
    assert local[1].load(project.value) == current


def test_changed_current_profile_does_not_relabel_a_historical_resolve(local):
    migrated = migrate(local)
    service, project, destination = local[6:]
    profile = migrated.state.profile(destination)
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    assert service.write(project, 3, destination, replace(profile, style='academic'), profile.revision).outcome is ProfileOutcome.UPDATED
    stale = service.resolve(project, 2, destination, profile.revision, plan())
    assert stale.outcome is ProfileOutcome.CONFLICT and stale.snapshot is None
    assert service.repository.read(project).snapshot(resolved.snapshot.reference) == resolved.snapshot


def test_successful_atomic_publication_preserves_restrictive_manifest_mode_without_hardlinks(local, monkeypatch):
    path = local[1]._manifest_path(local[7].value)
    path.chmod(0o400)
    def no_link(*args, **kwargs):
        raise AssertionError('Project profile publication must not need hard links')
    monkeypatch.setattr(os, 'link', no_link)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.MIGRATED
    assert path.stat().st_mode & 0o777 == 0o400


def test_corrupt_or_unknown_persisted_state_fails_closed(local):
    assert migrate(local).outcome is ProfileOutcome.MIGRATED
    projects, project = local[1], local[7]
    pointer = dict(projects.load(project.value).metadata)[STATE_KEY]
    path = projects._manifest_path(project.value).parent / 'profiles' / (pointer.split(':')[1] + '.json')
    prior = path.read_bytes()
    path.write_bytes(prior.replace(b'"version":1', b'"unknown":null,"version":1', 1))
    with pytest.raises(ProfileError, match='invalid_profile'):
        local[6].repository.read(project)
