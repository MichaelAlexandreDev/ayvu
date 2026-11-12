from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from ayvu.projects.profiles.service import ProfileOutcome
from ayvu.projects.profiles.store import STATE_KEY
from ayvu.projects.profiles.model import ProfileError
from ayvu.kernel import LanguagePair

from tests.projects.profiles.project_profile_integration_support import local, migrate, plan


def test_migration_and_style_remain_inert_over_instrumented_libretranslate_transport(local, monkeypatch):
    # Real requests prepares the payload; only its final send boundary is replaced.
    # Migration, snapshot publication and cache replay must not reach transport.
    import requests
    from ayvu.preferences.config_facade import TranslationProfile
    from ayvu.interfaces.cli.translate_options import _resolve_target_language, _resolve_glossary_path
    from ayvu.translation.pipeline import LegacyTextPipelineFacade, LinguisticSource
    from ayvu.providers.libretranslate.client import LibreTranslateTranslator
    from tests.support.translation_pipeline_support import Cache, Glossed
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
