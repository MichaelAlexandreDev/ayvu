"""Translator and model choices cross the real CLI, job and provider boundaries (#377)."""
import json

import pytest

from ayvu.interfaces.cli.app import app
from ayvu.jobs import JobState
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs


@pytest.mark.parametrize('arguments,profile,label', [
    (['--model', 'flag-model'], {}, 'flag-model'),
    (['--profile', 'chosen'], {'translator': 'libretranslate', 'model': 'profile-model'}, 'profile-model'),
    (['--profile', 'chosen', '--model', 'flag-model'],
     {'translator': 'ollama', 'model': 'qwen2.5:7b'}, 'flag-model'),
])
def test_model_selection_and_explicit_profile_override(tmp_path, monkeypatch, arguments, profile, label):
    config(tmp_path, label='config-model', profiles={'chosen': profile})
    source = _book(tmp_path / 'book.epub')
    result, _ = invoke(tmp_path, monkeypatch, source, *arguments,
                       *(['--translator', 'libretranslate'] if profile.get('translator') == 'ollama' else []))
    assert result.exit_code == 0, text(result)
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == 'translate_project')
    assert {(r.producer.provider, r.producer.model) for r in records(tmp_path, job)} == {
        ('libretranslate', f'declared:{label}')}
    plan, report = text(result).split('Translation report')
    assert 'Provider' in plan and 'libretranslate' in plan and label in plan
    assert 'Provider' in report and 'libretranslate' in report and label in report
    data = json.loads((tmp_path / 'config/ayvu/config.json').read_text())
    assert data['translator_models'] == {'libretranslate': 'config-model'}


@pytest.mark.parametrize('profile', [False, True], ids=['flag', 'profile'])
def test_invalid_model_stops_before_provider_or_project(tmp_path, monkeypatch, profile):
    config(tmp_path, profiles={'bad': {'translator': 'libretranslate', 'model': 'Bad Model'}} if profile else {})
    source = _book(tmp_path / 'book.epub')
    result, session = invoke(tmp_path, monkeypatch, source,
                             *(['--profile', 'bad'] if profile else ['--model', 'Bad Model']))
    assert result.exit_code == 1, text(result)
    assert 'model' in text(result).lower() and 'invalid' in text(result).lower()
    assert session.requests == []
    assert not (tmp_path / 'processing').exists()


def test_resume_keeps_original_model_after_configuration_and_profile_change(tmp_path, monkeypatch):
    path = config(tmp_path, label='config-model', profiles={
        'chosen': {'translator': 'libretranslate', 'model': 'original-model'}})
    source = _book(tmp_path / 'book.epub')
    failed, _ = invoke(tmp_path, monkeypatch, source, '--profile', 'chosen', '--fail-fast',
                        session=Transport(failing='Goodbye reader.'))
    assert failed.exit_code == 1, text(failed)
    parent = next(job for job in _jobs(tmp_path) if job.request.command_type == 'translate_project')
    data = json.loads(path.read_text())
    data.update(translator_models={'ollama': 'other:1b'}, profiles={})
    path.write_text(json.dumps(data))
    session = Transport()
    monkeypatch.setattr('ayvu.providers.libretranslate.client.requests.Session', lambda: session)
    result = runner.invoke(app, ['--mode', 'developer', 'resume', str(source)])
    assert result.exit_code == 0, text(result)
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == parent.job_id)
    assert child.state is JobState.VALIDATED
    assert {(r.producer.provider, r.producer.model) for r in records(tmp_path, child)} == {
        ('libretranslate', 'declared:original-model')}
    assert 'original-model' in text(result) and 'other:1b' not in text(result)
