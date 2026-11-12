from dataclasses import replace

import pytest

from ayvu.knowledge.cache import OpaqueSemanticRevision, SemanticOperationV2, SemanticRouteHop
from ayvu.preferences.model import GlobalPreferences, NamedPreferenceProfile, PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from ayvu.provenance.model import ProducerIdentity
from ayvu.projects.profiles.model import ExecutionStage, ProfileError, ProfileOverrides, ProjectTranslationProfile
from ayvu.projects.profiles.service import ProfileOutcome

from tests.support.project_profiles_support import PROFILE, PROJECT, Preferences, REV, binding
from tests.support.project_profiles_support import facts, migrate, service, snapshot


def test_migration_exact_owner_handoff_idempotence_and_divergent_name_conflict(service):
    prefs = Preferences()
    result = migrate(service, prefs)
    assert result.outcome is ProfileOutcome.MIGRATED
    profile = result.state.profile(PROFILE)
    assert profile == ProjectTranslationProfile('fr', service.glossaries.resolved, 'literary')
    assert service.glossaries.requests == ['legacy-private/path.json']
    assert service.glossaries.held == [(profile.glossary,)]
    assert migrate(service, prefs).outcome is ProfileOutcome.ALREADY_MIGRATED
    assert migrate(service, Preferences(NamedPreferenceProfile('es'))).outcome is ProfileOutcome.CONFLICT
    assert prefs.saved == 0 and service.repository.writes == 1
    assert 'private-name-canary' not in str(result.state.to_mapping())
    assert 'legacy-private' not in str(result.state.to_mapping())


@pytest.mark.parametrize('reason', ['missing', 'external', 'ambiguous', 'unsafe', 'stale', 'io'])
def test_blocked_glossary_migration_has_typed_reason_and_no_write(service, reason):
    service.glossaries.block = reason
    result = migrate(service)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY
    assert result.glossary_failure == reason
    assert result.state is None and service.repository.writes == 0


def test_migration_default_is_not_a_caller_override(service):
    result = migrate(service, Preferences(NamedPreferenceProfile()))
    assert result.state.profile(PROFILE).target_language is None
    resolved = service.resolve(PROJECT, 2, PROFILE, result.state.profile(PROFILE).revision, facts('de'), ProfileOverrides('de'))
    assert resolved.snapshot.target_language == 'de'
    assert resolved.snapshot.profile_value.target_language is None


@pytest.mark.parametrize('kind', ['stale_project', 'cancel', 'persistence', 'unknown_profile', 'unknown_source', 'unsupported'])
def test_no_partial_migration_on_failure(service, kind):
    prefs = Preferences()
    cancel = None
    expected = 1
    if kind == 'stale_project':
        expected = 2
    elif kind == 'cancel':
        cancel = lambda: True
    elif kind == 'persistence':
        service.repository.fail = True
    elif kind == 'unknown_profile':
        prefs.load = lambda: PreferencesSnapshot(GlobalPreferences(), 'a' * 64, True)
    elif kind == 'unknown_source':
        original = prefs.load()
        prefs.load = lambda: replace(original, read_only=True, warnings=('unknown_fields',))
    else:
        prefs.load = lambda: PreferencesSnapshot(None, None, True, error=PreferencesError('unsupported_version'))
    prior = service.repository.state
    result = service.migrate(prefs, 'private-name-canary', PROJECT, expected, PROFILE, cancel)
    assert result.outcome.value == {'stale_project': 'conflict', 'cancel': 'cancelled', 'persistence': 'persistence',
        'unknown_profile': 'invalid_profile', 'unknown_source': 'invalid_profile', 'unsupported': 'unsupported_version'}[kind]
    assert service.repository.state == prior and service.repository.writes == 0
    assert result.snapshot is None and result.state is None


def test_version_update_historical_snapshot_and_snapshot_idempotence(service):
    created = service.write(PROJECT, 1, PROFILE, ProjectTranslationProfile())
    assert created.outcome is ProfileOutcome.CREATED
    resolved = service.resolve(PROJECT, 2, PROFILE, created.state.profile(PROFILE).revision, facts())
    assert resolved.outcome is ProfileOutcome.RESOLVED
    assert service.resolve(PROJECT, 2, PROFILE, created.state.profile(PROFILE).revision, facts()).snapshot == resolved.snapshot
    updated = service.write(PROJECT, 3, PROFILE, ProjectTranslationProfile(style='literary'), created.state.profile(PROFILE).revision)
    assert updated.outcome is ProfileOutcome.UPDATED
    assert len(updated.state.records) == 2
    assert updated.state.snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert updated.state.profile(PROFILE, created.state.profile(PROFILE).revision) == created.state.profile(PROFILE)
    assert service.write(PROJECT, 3, PROFILE, ProjectTranslationProfile('es'), created.state.profile(PROFILE).revision).outcome is ProfileOutcome.CONFLICT
    assert service.resolve(PROJECT, 4, PROFILE, created.state.profile(PROFILE).revision, facts()).outcome is ProfileOutcome.CONFLICT


def test_153_consumer_receives_only_raw_bounded_durable_projection(service):
    created = service.write(PROJECT, 1, PROFILE, ProjectTranslationProfile(glossary=binding(), style='literary'))
    resolved = service.resolve(PROJECT, 2, PROFILE, created.state.profile(PROFILE).revision, facts(glossary=binding()))
    arguments = resolved.snapshot.raw_semantic_contribution()
    revision = OpaqueSemanticRevision(**arguments)
    assert revision.snapshot_reference is not None
    assert resolved.state.raw_projection(revision.snapshot_reference) == resolved.snapshot.effects.raw_projection()
    assert b'canary' not in resolved.state.raw_projection(revision.snapshot_reference)
    with pytest.raises(ProfileError, match='not_found'):
        resolved.state.raw_projection('profile_effect:' + 'f' * 64)


def test_153_real_semantic_fingerprint_omits_downstream_style_and_operational_changes():
    def consumer(value):
        contribution = OpaqueSemanticRevision(**value.raw_semantic_contribution())
        baseline = OpaqueSemanticRevision('ayvu', 'pipeline', 1, 'baseline-v1')
        hop = SemanticRouteHop('en', value.target_language, ProducerIdentity('libretranslate', 'model', 'adapter-v1', 'pipeline-v1'),
                               'capfp:v1:' + 'a' * 64, contribution)
        return SemanticOperationV2('translation', 'en', value.target_language, (hop,), baseline,
            (('provider', 1),), 'none', None, 'none', None, None, (), baseline, 3000,
            baseline, baseline, baseline).fingerprint
    old = snapshot(ProjectTranslationProfile(glossary=binding(), style='literary'), facts(glossary=binding()))
    changed = snapshot(ProjectTranslationProfile(glossary=binding('c'), style='technical'),
                       facts(glossary=binding('c'), stage=ExecutionStage.REPLAY))
    assert consumer(old) == consumer(changed)
    native = replace(old, facts=facts(glossary=binding(), native=True))
    native_changed = replace(changed, facts=facts(glossary=binding('c'), native=True))
    assert consumer(native) != consumer(native_changed) != consumer(old)


@pytest.mark.parametrize('revision', [True, None, '1', 0, -1, 2**63])
def test_invalid_application_request_types_never_publish(service, revision):
    prior = service.repository.state
    result = service.write(PROJECT, revision, PROFILE, ProjectTranslationProfile())
    assert result.outcome is ProfileOutcome.INVALID_PROFILE
    assert service.repository.state == prior and service.repository.writes == 0


def test_unknown_profile_revision_is_a_conflict_with_no_snapshot(service):
    service.write(PROJECT, 1, PROFILE, ProjectTranslationProfile())
    result = service.resolve(PROJECT, 2, PROFILE, REV, facts())
    assert result.outcome is ProfileOutcome.CONFLICT and result.snapshot is None
    assert service.repository.state.snapshots == ()


def test_cancelled_migration_stops_before_reading_any_owner(service):
    events = []
    prefs = Preferences()
    def unexpected(*args):
        events.append('owner_read')
        raise AssertionError('Pre-publication cancellation must stop at entry')
    prefs.load = unexpected
    service.repository.read = unexpected
    service.glossaries.resolve_legacy = unexpected
    result = service.migrate(prefs, 'private-name-canary', PROJECT, 1, PROFILE, lambda: True)
    assert result.outcome is ProfileOutcome.CANCELLED
    assert result.state is None and result.snapshot is None and events == []
