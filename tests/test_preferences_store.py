from dataclasses import replace
import json
import multiprocessing
import os

import pytest

from ayvu.preferences import GlobalPreferences
from ayvu.preferences_service import GetPreferences, UpdatePreferences, get_preferences, update_preferences
from ayvu.preferences_store import JsonPreferencesRepository


def test_missing_get_is_read_only_and_update_keeps_v1_format(tmp_path):
    path = tmp_path / 'nested' / 'config.json'
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository, GetPreferences())
    assert loaded.preferences == GlobalPreferences()
    assert loaded.revision == 'missing' and not loaded.exists
    assert not path.parent.exists()
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert result.persisted
    assert json.loads(path.read_text())['version'] == 1
    assert 'revision' not in json.loads(path.read_text())
    assert get_preferences(repository).revision == result.snapshot.revision


@pytest.mark.parametrize('extension', [
    {'future': {'private-canary': 'must-survive'}},
    {'folders': {'future': 'private-canary'}},
    {'profiles': {'test': {'future': 'private-canary'}}},
])
def test_unknown_fields_load_read_only_and_refuse_rewrite(tmp_path, extension):
    path = tmp_path / 'config.json'
    payload = json.dumps({'version': 1, **extension}).encode()
    path.write_bytes(payload)
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    assert loaded.preferences is not None and loaded.read_only
    assert loaded.warnings == ('unknown_fields',)
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert not result.persisted and result.error.code == 'unknown_fields'
    assert path.read_bytes() == payload
    assert 'private-canary' not in repr(loaded) + repr(result)


def test_stale_revision_never_overwrites_successful_update(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"version":1}')
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    first = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    before = path.read_bytes()
    second = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='fr'), loaded.revision))
    assert first.persisted
    assert not second.persisted and second.error.code == 'conflict'
    assert path.read_bytes() == before
    assert second.effective.default_target_language == 'fr'


def test_legacy_underscore_language_round_trips_on_unrelated_update(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"version":1,"default_target_language":"pt_BR"}')
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    assert loaded.error is None
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, reader_app='foliate'), loaded.revision))
    assert result.persisted
    assert json.loads(path.read_text())['default_target_language'] == 'pt_BR'


@pytest.mark.parametrize('command', [
    'reader --token secret-canary', 'reader "--token" "secret-canary"',
    'TOKEN=secret-canary reader', 'reader --api-key=secret-canary',
    'reader https://user:secret-canary@example.invalid',
    'reader --client-secret secret-canary',
    'reader --bearer-token secret-canary',
    'reader --refresh-token secret-canary',
])
def test_credential_commands_never_enter_persistence(tmp_path, command):
    from ayvu.preferences import PreferencesError
    with pytest.raises(PreferencesError) as caught:
        replace(GlobalPreferences(), reader_app=command)
    assert 'secret-canary' not in str(caught.value)


@pytest.mark.parametrize('payload,code', [
    (b'{bad', 'malformed'), (b'[]', 'invalid'),
    (b'{"version":true}', 'invalid'), (b'{"version":99}', 'unsupported_version'),
    (b'{"version":1,"version":1}', 'malformed'),
    (b'{"version":1,"future":NaN}', 'malformed'),
    (b' ' * (128 * 1024 + 1), 'oversized'),
    (b'{"folders":[]}', 'invalid'), (b'{"profiles":[]}', 'invalid'),
])
def test_invalid_file_outcomes_are_distinct_and_never_mutate(tmp_path, payload, code):
    path = tmp_path / 'config.json'
    path.write_bytes(payload)
    result = get_preferences(JsonPreferencesRepository(path))
    assert result.error.code == code
    assert result.preferences is None
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('operation', ['replace', 'fsync'])
@pytest.mark.parametrize('error_type,code', [(OSError, 'io'), (PermissionError, 'permission')])
def test_atomic_failure_preserves_old_bytes_and_unpersisted_intent(tmp_path, monkeypatch, operation, error_type, code):
    path = tmp_path / 'config.json'
    path.write_text('{"version":1}')
    repository = JsonPreferencesRepository(path)
    original = path.read_bytes()
    loaded = get_preferences(repository)
    def fail(*args):
        raise error_type('private-canary-path')
    monkeypatch.setattr('ayvu.preferences_store.os.' + operation, fail)
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert not result.persisted and result.error.code == code
    assert result.effective.default_target_language == 'es'
    assert path.read_bytes() == original
    assert 'private-canary' not in repr(result) + str(result.error)
    assert not list(tmp_path.glob('*.tmp'))


def _process_update(path, barrier, queue, language):
    from pathlib import Path
    repository = JsonPreferencesRepository(Path(path))
    snapshot = get_preferences(repository)
    barrier.wait(timeout=10)
    result = update_preferences(repository, UpdatePreferences(
        replace(snapshot.preferences, default_target_language=language), snapshot.revision))
    queue.put((result.persisted, None if result.persisted else result.error.code))


def test_independent_writers_with_same_revision_have_one_winner(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"version":1}')
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(3)
    queue = context.Queue()
    processes = [context.Process(target=_process_update, args=(str(path), barrier, queue, lang))
                 for lang in ('es', 'fr')]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(results) == [(False, 'conflict'), (True, None)]
        assert json.loads(path.read_text())['default_target_language'] in ('es', 'fr')
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


def test_lock_contention_has_deterministic_busy_outcome(tmp_path, monkeypatch):
    from ayvu.preferences_store import _exclusive_lock
    path = tmp_path / 'config.json'
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    with _exclusive_lock(tmp_path / '.config.json.lock'):
        with monkeypatch.context() as patch:
            clock = iter([0, 10])
            patch.setattr('ayvu.preferences_store.time.monotonic', lambda: next(clock))
            result = update_preferences(repository, UpdatePreferences(loaded.preferences, loaded.revision))
    assert result.error.code == 'busy'
    assert not path.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory'])
def test_unsafe_config_entries_are_rejected_without_touching_target(tmp_path, kind):
    source = tmp_path / 'source.json'
    source.write_text('{"version":1}')
    path = tmp_path / 'config.json'
    if kind == 'symlink':
        path.symlink_to(source)
    elif kind == 'hardlink':
        os.link(source, path)
    else:
        path.mkdir()
    loaded = get_preferences(JsonPreferencesRepository(path))
    assert loaded.error.code == 'unsafe_path'
    assert source.read_text() == '{"version":1}'


def test_permission_outcome_does_not_echo_raw_exception(tmp_path, monkeypatch):
    def fail(*args):
        raise PermissionError('private-canary-path')
    monkeypatch.setattr('ayvu.preferences_store.os.open', fail)
    loaded = get_preferences(JsonPreferencesRepository(tmp_path / 'config.json'))
    assert loaded.error.code == 'permission'
    assert 'private-canary' not in str(loaded.error) + repr(loaded)


def test_post_publication_sync_failure_requires_reload(tmp_path, monkeypatch):
    if os.name == 'nt':
        return  # Windows has no directory-fsync implementation to exercise.
    path = tmp_path / 'config.json'
    path.write_text('{"version":1}')
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    original = os.fsync
    calls = []
    def fail_second(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError('synthetic-post-publication-failure')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr('ayvu.preferences_store.os.fsync', fail_second)
        result = update_preferences(repository, UpdatePreferences(
            replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert not result.persisted
    assert result.error.code == 'commit_uncertain'
    reloaded = get_preferences(repository)
    assert reloaded.preferences.default_target_language == 'es'
    assert reloaded.revision != loaded.revision


def test_known_profile_fields_and_authorized_paths_are_lossless(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'version': 1, 'books_dir': '/private/Token Books',
        'reader_app': 'reader --new-window', 'profiles': {
            'technical': {'target_language': 'zh_CN', 'glossary': '../terms.json', 'style': 'technical'},
            'empty': {},
        }}))
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    assert loaded.error is None
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, default_target_language='es'), loaded.revision))
    assert result.persisted
    payload = json.loads(path.read_text())
    assert payload['profiles']['technical'] == {
        'target_language': 'zh_CN', 'glossary': '../terms.json', 'style': 'technical'}
    assert payload['profiles']['empty'] == {}
    assert payload['books_dir'] == '/private/Token Books'
    assert '/private' not in repr(loaded) + repr(result) + repr(result.effective)


def test_shared_writable_parent_is_refused_before_mutation(tmp_path):
    if os.name == 'nt':
        return
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    path = shared / 'config.json'
    path.write_text('{"version":1}')
    original = path.read_bytes()
    repository = JsonPreferencesRepository(path)
    loaded = get_preferences(repository)
    result = update_preferences(repository, UpdatePreferences(
        replace(loaded.preferences, reader_app='reader'), loaded.revision))
    assert result.error.code == 'unsafe_path'
    assert path.read_bytes() == original
    assert not list(shared.glob('.*'))
