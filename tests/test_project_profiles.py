"""Pure contract tests: #134/#152/#153/#157 owners are supplied through ports."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ayvu.knowledge.cache import OpaqueSemanticRevision, SemanticOperationV2, SemanticRouteHop
from ayvu.knowledge.glossary.catalog import GlossaryReference
from ayvu.preferences.model import GlobalPreferences, NamedPreferenceProfile, PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from ayvu.provenance.model import ProducerIdentity
from ayvu.projects.profiles.model import (
    ExecutionStage, GlossaryBinding, GlossaryStage, PlanFacts, ProfileError, ProfileOverrides,
    ProfileReference, ProfileRevision, ProjectReference, ProjectTranslationProfile,
    ProviderRequestProjection, ResolvedProfileSnapshot, SemanticEffect, SnapshotReference,
    TerminologySupport, canonical,
)
from ayvu.projects.profiles.service import (
    ProfileOutcome, ProfileRecord, ProjectProfileService, ProjectProfileState,
)
from ayvu.projects.profiles.store import decode_state, encode_state

from tests.support.project_profiles_support import PROFILE, PROJECT, Preferences, REV, binding
from tests.support.project_profiles_support import facts, migrate, service, snapshot


@pytest.mark.parametrize('change', [
    {'version': 0}, {'version': 2}, {'version': True}, {'version': '1'},
    {'target_language': 1}, {'target_language': ''}, {'target_language': 'x' * 36},
    {'style': 'activate'}, {'style': []}, {'glossary': 'private/path.json'},
    {'glossary': {'reference': '../canary.json', 'revision': 'a' * 64}},
    {'glossary': {'reference': 'ok.json', 'revision': 'a'}},
    {'glossary': {'reference': 'ok.json', 'revision': 'a' * 64, 'path': 'canary'}},
    {'tone': 'formal'}, {'audience': None},
])
def test_closed_profile_schema(change):
    data = ProjectTranslationProfile().to_mapping() | change
    with pytest.raises(ProfileError) as caught:
        ProjectTranslationProfile.from_mapping(data)
    expected = 'unsupported_version' if type(change.get('version')) is int else 'invalid_profile'
    assert caught.value.code == expected
    assert 'canary' not in str(caught.value)


@pytest.mark.parametrize('missing', ['version', 'target_language', 'glossary', 'style'])
def test_profile_nullable_fields_are_required(missing):
    data = ProjectTranslationProfile().to_mapping()
    del data[missing]
    with pytest.raises(ProfileError, match='invalid_profile'):
        ProjectTranslationProfile.from_mapping(data)


def test_canonical_profile_order_revision_and_immutability():
    profile = ProjectTranslationProfile('fr', binding(), 'literary')
    data = profile.to_mapping()
    reopened = ProjectTranslationProfile.from_mapping(dict(reversed(list(data.items()))))
    assert reopened == profile
    assert reopened.canonical_payload() == profile.canonical_payload()
    assert reopened.revision == profile.revision
    for changed in (replace(profile, target_language='es'), replace(profile, style='technical'),
                    replace(profile, glossary=binding('c'))):
        assert changed.revision != profile.revision
    with pytest.raises(FrozenInstanceError):
        profile.style = 'neutral'
    assert canonical({'a': None, 'b': True}) == canonical({'b': True, 'a': None})
    assert len({canonical(v) for v in (None, False, 0, '0', (), {})}) == 6


@pytest.mark.parametrize('target,stored,default,effective', [
    (None, 'fr', 'pt', 'fr'), (' es ', 'fr', 'pt', 'es'), (' ', 'fr', 'pt', 'fr'),
    (None, None, 'pt', 'pt'), (None, None, 'de', 'de'), (None, ' fr ', 'pt', ' fr '),
])
def test_target_precedence_and_legacy_default_intent(target, stored, default, effective):
    value = snapshot(ProjectTranslationProfile(stored), facts(effective), ProfileOverrides(target), default)
    assert value.target_language == effective
    assert value.profile_value.target_language == stored
    assert value.to_mapping()['overrides']['target_language'] == (target.strip() or None if target else None)


def test_glossary_override_presence_and_explicit_disable():
    old, new = binding(), binding('c')
    profile = ProjectTranslationProfile(glossary=old)
    assert snapshot(profile, facts(glossary=old)).glossary == old
    resolved = snapshot(profile, facts(glossary=new), ProfileOverrides(glossary_present=True, glossary=new))
    assert resolved.glossary == new and resolved.profile_value.glossary == old
    assert snapshot(profile, facts(), ProfileOverrides(glossary_present=True)).glossary is None
    with pytest.raises(ProfileError, match='invalid_profile'):
        ProfileOverrides(glossary=new)
    with pytest.raises(ProfileError, match='invalid_profile'):
        ProfileOverrides(glossary_present=1)


def test_all_effect_classes_and_raw_downstream_invalidation():
    first = snapshot(ProjectTranslationProfile(glossary=binding(), style='literary'), facts(glossary=binding()))
    second = snapshot(ProjectTranslationProfile(glossary=binding('c'), style='technical'), facts(glossary=binding('c')))
    classes = {effect for _, effect, _ in first.effects.dimensions}
    assert classes == set(SemanticEffect)
    assert first.effects.raw_projection() == second.effects.raw_projection()
    assert first.raw_semantic_contribution() == second.raw_semantic_contribution()
    assert first.effects.downstream_projection() != second.effects.downstream_projection()
    assert first.reference != second.reference
    native1 = snapshot(first.profile_value, facts(glossary=binding(), native=True))
    native2 = snapshot(second.profile_value, facts(glossary=binding('c'), native=True))
    assert native1.effects.raw_projection() != native2.effects.raw_projection()
    assert native1.effects.raw_projection() != first.effects.raw_projection()
    assert native1.effects.downstream_projection() == native2.effects.downstream_projection()
    assert b'canary-glossary' not in native1.effects.raw_projection()
    assert dict(native1.effects.partition(SemanticEffect.RAW_PROVIDER))['glossary'].startswith('glossary-effect:')


def test_style_never_changes_provider_input_or_raw_cache_contribution():
    values = [snapshot(ProjectTranslationProfile(style=label)) for label in (None, 'neutral', 'academic', 'technical', 'literary')]
    assert len({v.profile_value.revision for v in values}) == 5
    assert len({v.reference for v in values}) == 5
    assert all(v.facts.provider == values[0].facts.provider for v in values)
    assert len({v.effects.raw_projection() for v in values}) == 1
    assert all(v.raw_semantic_contribution() == values[0].raw_semantic_contribution() for v in values)
    replay = replace(values[0], facts=facts(stage=ExecutionStage.REPLAY))
    assert replay.effects.raw_projection() == values[0].effects.raw_projection()
    assert replay.reference != values[0].reference
    inspected = replace(values[0], facts=facts(stage=ExecutionStage.INSPECT))
    assert inspected.effects.partition(SemanticEffect.RAW_PROVIDER) == ()


def test_capability_and_exact_terminology_policy_contribute_only_when_sent():
    value = snapshot(ProjectTranslationProfile(glossary=binding()), facts(glossary=binding(), native=True))
    for request in (replace(value.facts.provider, capability_revision=ProfileRevision('d' * 64)),
                    replace(value.facts.provider, terminology_policy=ProfileRevision('e' * 64))):
        assert replace(value, facts=replace(value.facts, provider=request)).effects.raw_projection() != value.effects.raw_projection()


@pytest.mark.parametrize('make', [
    lambda: PlanFacts('translate', GlossaryStage.NONE, facts().provider),
    lambda: PlanFacts(ExecutionStage.TRANSLATE, GlossaryStage.NONE, None),
    lambda: PlanFacts(ExecutionStage.INSPECT, GlossaryStage.NONE, facts().provider),
    lambda: PlanFacts(ExecutionStage.TRANSLATE, GlossaryStage.PROVIDER, facts().provider),
    lambda: ProviderRequestProjection('pt', REV, TerminologySupport.UNKNOWN, binding(), REV),
    lambda: ProviderRequestProjection('pt', REV, TerminologySupport.SUPPORTED, binding()),
    lambda: snapshot(ProjectTranslationProfile('fr'), facts('pt')),
    lambda: snapshot(ProjectTranslationProfile(), facts(glossary=binding())),
    lambda: snapshot(ProjectTranslationProfile(glossary=binding('c')), facts(glossary=binding(), native=True)),
])
def test_incomplete_or_inconsistent_classification_fails_closed(make):
    with pytest.raises(ProfileError, match='invalid_plan'):
        make()


def test_snapshot_closed_effects_cannot_be_relabeled_by_metadata():
    value = snapshot()
    for mutate in (
        lambda d: d.update(provider_metadata={'style': 'raw_provider_affecting'}),
        lambda d: d['effects']['raw_provider_affecting'].update(style='literary'),
        lambda d: d['facts'].update(classification='inert'),
        lambda d: d['facts']['provider'].update(document='canary'),
        lambda d: d.update(profile_revision='b' * 64),
        lambda d: d.update(policy_version=2),
        lambda d: d.update(version=2),
    ):
        data = value.to_mapping()
        mutate(data)
        with pytest.raises(ProfileError):
            ResolvedProfileSnapshot.from_mapping(data)


def test_snapshot_identity_across_processes_and_mapping_order():
    value = snapshot(ProjectTranslationProfile(glossary=binding()), facts(glossary=binding()))
    data = value.to_mapping()
    reordered = dict(reversed(list(data.items())))
    assert ResolvedProfileSnapshot.from_mapping(reordered).reference == value.reference
    script = ('import json,sys; from ayvu.projects.profiles.model import ResolvedProfileSnapshot; '
              's=ResolvedProfileSnapshot.from_mapping(json.load(sys.stdin)); '
              'print(s.reference.value); print(s.effects.raw_projection().hex())')
    completed = subprocess.run([sys.executable, '-c', script], input=json.dumps(reordered), text=True,
                               capture_output=True, check=True)
    assert completed.stdout.splitlines() == [value.reference.value, value.effects.raw_projection().hex()]
    assert replace(value, project_revision=4).reference != value.reference
    assert replace(value, overrides=ProfileOverrides('pt')).reference != value.reference


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


def test_serialization_rejects_duplicates_missing_fields_and_non_json_values(service):
    state = migrate(service).state
    payload = encode_state(state)
    assert decode_state(payload) == state
    for invalid in (payload.replace(b'"version":1', b'"version":1,"version":1', 1),
                    payload.replace(b'"version":1', b'"version":NaN', 1),
                    payload.replace(b'"version":1', b'"version":2', 1)):
        with pytest.raises(ProfileError):
            decode_state(invalid)
    for field in state.to_mapping():
        data = state.to_mapping()
        del data[field]
        with pytest.raises(ProfileError):
            ProjectProfileState.from_mapping(data)
    with pytest.raises(ProfileError, match='invalid_profile'):
        replace(state, heads=state.heads + state.heads)
    with pytest.raises(ProfileError, match='invalid_profile'):
        replace(state, records=state.records + state.records)


def test_sensitive_values_never_enter_reprs_default_diagnostics_or_failures(service, caplog):
    migrated = migrate(service)
    value = migrated.state.profile(PROFILE)
    resolved = service.resolve(PROJECT, 2, PROFILE, value.revision, facts('fr', glossary=binding()))
    secret_values = ('private-name-canary', 'legacy-private/path.json', 'canary-glossary.json', 'fr',
                     value.revision.value, binding().revision.value, resolved.snapshot.reference.value,
                     resolved.snapshot.raw_semantic_contribution()['value'])
    objects = (PROFILE, PROJECT, value, value.revision, binding(), binding().reference, migrated,
               migrated.state, resolved, resolved.snapshot, resolved.snapshot.reference, resolved.snapshot.effects,
               ProfileError('private-name-canary', 'legacy-private/path.json'))
    diagnostics = ' '.join(repr(o) for o in objects)
    diagnostics += str([o.diagnostic() for o in objects if hasattr(o, 'diagnostic')]) + caplog.text
    for canary in secret_values:
        assert canary not in diagnostics


def test_contract_modules_have_no_concrete_or_presentation_imports():
    root = Path(__file__).resolve().parents[1] / 'src' / 'ayvu'
    forbidden = {'typer', 'rich', 'json', 'pathlib', 'os', 'sqlite3', 'PySide6',
                 'project_store', 'profile_glossary_store', 'project_profile_store', 'provider_libretranslate'}
    for filename in ('projects/profiles/model.py', 'projects/profiles/service.py'):
        tree = ast.parse((root / filename).read_text())
        imports = {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        imports |= {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not imports & forbidden


def test_reference_and_collection_bounds_are_closed():
    for factory, value in ((ProjectReference, 'private/path'), (ProfileReference, 'display-name'),
                           (ProfileRevision, 'a' * 65), (SnapshotReference, False),
                           (ProfileReference, None)):
        with pytest.raises(ProfileError, match='invalid_profile'):
            factory(value)
    with pytest.raises(ProfileError, match='invalid_profile'):
        ProjectTranslationProfile(glossary=GlossaryBinding.from_mapping({'reference': 'a' * 251 + '.json', 'revision': 'a' * 64}))
    records = tuple(ProfileRecord(ProfileReference('profile:' + f'{i:064x}'), ProjectTranslationProfile()) for i in range(65))
    with pytest.raises(ProfileError, match='limit'):
        ProjectProfileState(PROJECT, 1, records, tuple((r.reference, r.value.revision) for r in records))
    with pytest.raises(ProfileError, match='limit'):
        ProjectProfileState(PROJECT, 1, (records[0],) * 1025)
