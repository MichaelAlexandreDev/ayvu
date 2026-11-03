from dataclasses import replace
import json
from pathlib import Path

import pytest

from ayvu.preferences import GlobalPreferences
from ayvu.preferences_service import GetPreferences, UpdatePreferences, get_preferences, update_preferences


def test_framework_free_consumer_uses_repository_contract():
    from ayvu.preferences_service import PreferencesSnapshot
    class Repository:
        def load(self):
            return PreferencesSnapshot(GlobalPreferences(), 'missing', False)
        def save(self, preferences, expected_revision):
            assert expected_revision == 'missing'
            return PreferencesSnapshot(preferences, 'a' * 64, True)
    repository = Repository()
    loaded = get_preferences(repository, GetPreferences())
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert result.persisted
    assert result.effective.default_target_language == 'es'
    assert result.snapshot.exists


def test_failed_update_keeps_intent_and_reports_persistence_separately():
    from ayvu.preferences import PreferencesError
    class Repository:
        def save(self, preferences, expected_revision):
            raise PreferencesError('conflict')
    intent = replace(GlobalPreferences(), books_dir='/private/reading')
    result = update_preferences(Repository(), UpdatePreferences(intent, 'a' * 64))
    assert not result.persisted
    assert result.effective == intent
    assert result.error.code == 'conflict'
    assert '/private' not in repr(result)


def test_field_classification_is_complete_executable_contract():
    from ayvu.preferences import NamedPreferenceProfile, preferences_to_mapping
    contract = json.loads((Path(__file__).parents[1] /
        'docs/architecture/global-preferences-fields.json').read_text())
    fields = {entry['path']: entry for entry in contract['fields']}
    preferences = replace(GlobalPreferences(), profiles=(('test', NamedPreferenceProfile('pt', 'terms.json', 'technical')),))
    payload = preferences_to_mapping(preferences)
    paths = set(payload)
    paths.update('folders.' + key for key in payload['folders'])
    paths.add('profiles.*')
    paths.update('profiles.*.' + key for key in payload['profiles']['test'])
    assert paths == set(fields)
    for entry in fields.values():
        assert {'path', 'owner', 'validation', 'sensitivity', 'default', 'compatibility', 'consumer'} <= entry.keys()
        assert entry['owner'] in {'global_preference', 'named_profile', 'schema'}
    assert {entry['owner'] for entry in contract['excluded']} >= {'project', 'presentation', 'credentials'}


@pytest.mark.parametrize('field', ['project_id', 'provider', 'network_policy', 'token', 'window_geometry'])
def test_unowned_fields_cannot_enter_preferences_dto(field):
    with pytest.raises(TypeError):
        GlobalPreferences(**{field: 'synthetic-canary'})
