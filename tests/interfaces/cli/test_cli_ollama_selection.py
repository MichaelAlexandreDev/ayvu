"""Ollama flag, profile, configuration and continuation choices through real composition (#377)."""
import json

import pytest

from ayvu.interfaces.cli.app import app
from tests.support.cli_rich_runtime_support import config, isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.ollama_selection_support import OllamaTransport
from tests.support.ollama_fakes import MODEL, DIGEST_A


@pytest.mark.parametrize('choice', ['flag', 'profile', 'configuration'])
def test_ollama_selection_uses_the_selected_model_and_default_url(tmp_path, monkeypatch, choice):
    models = {'ollama': MODEL} if choice == 'configuration' else {}
    profile = {'chosen': {'translator': 'ollama', 'model': MODEL}}
    config(tmp_path, label=None, translator_models=models, profiles=profile)
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    options = ['--profile', 'chosen'] if choice == 'profile' else ['--translator', 'ollama']
    if choice == 'flag':
        options += ['--model', MODEL]
    result = runner.invoke(app, ['--mode', 'developer', 'translate', str(source), '--source', 'en',
                                 '--output', str(tmp_path / 'out.epub'), '--retries', '0', *options])
    assert result.exit_code == 0, text(result)
    assert all(url.startswith('http://localhost:11434/') for _, url, _ in session.requests)
    bodies = [body for method, _, body in session.requests if method == 'POST']
    assert bodies and all(body['model'] == MODEL for body in bodies)
    assert MODEL in text(result) and 'Provider' in text(result) and 'ollama' in text(result)


def test_ollama_resume_ignores_a_changed_configuration(tmp_path, monkeypatch):
    path = config(tmp_path, label=None, translator_models={'ollama': MODEL})
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport(failing=True)
    monkeypatch.setattr('requests.Session', lambda: session)
    args = ['--mode', 'developer', 'translate', str(source), '--translator', 'ollama', '--source', 'en',
            '--retries', '0', '--fail-fast', '--output', str(tmp_path / 'out.epub')]
    result = runner.invoke(app, args)
    assert result.exit_code == 1, text(result)
    parent = next(job for job in _jobs(tmp_path) if job.request.command_type == 'translate_project')
    data = json.loads(path.read_text())
    data['translator_models'] = {'ollama': 'llama3.2:3b', 'libretranslate': 'changed-model'}
    path.write_text(json.dumps(data))
    session.failing = False
    session.requests.clear()
    result = runner.invoke(app, ['--mode', 'developer', 'resume', str(source)])
    assert result.exit_code == 0, text(result)
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == parent.job_id)
    assert {(r.producer.provider, r.producer.model) for r in records(tmp_path, child)} == {
        ('ollama', f'{MODEL}@{DIGEST_A}')}
    assert all(body['model'] == MODEL for method, _, body in session.requests if method == 'POST')


@pytest.mark.parametrize('translator,model', [('ollama', 'bad model'), ('libretranslate', 'UPPER')])
def test_invalid_flag_model_is_rejected_without_http(tmp_path, monkeypatch, translator, model):
    config(tmp_path)
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['translate', str(source), '--translator', translator, '--model', model])
    assert result.exit_code == 1 and 'Invalid translator model' in text(result)
    assert session.requests == [] and not (tmp_path / 'processing').exists()


def test_preview_accepts_model_selection_and_refuses_ollama_without_http(tmp_path, monkeypatch):
    config(tmp_path, label=None)
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['--preview', str(source), '--translator', 'ollama', '--model', MODEL])
    assert result.exit_code == 1 and 'not available with Ollama yet' in text(result), text(result)
    assert session.requests == []


def test_resume_refuses_changed_ollama_weights_before_starting_a_new_job(tmp_path, monkeypatch):
    config(tmp_path, label=None, translator_models={'ollama': MODEL})
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport(failing=True)
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['translate', str(source), '--translator', 'ollama', '--fail-fast',
                                 '--retries', '0', '--output', str(tmp_path / 'out.epub')])
    assert result.exit_code == 1, text(result)
    before = [job.job_id for job in _jobs(tmp_path)]
    session.models = [(MODEL, 'c' * 64)]
    session.failing = False
    session.requests.clear()
    result = runner.invoke(app, ['--mode', 'developer', 'resume', str(source)])
    assert result.exit_code == 1 and 'installed model changed' in text(result), text(result)
    assert [job.job_id for job in _jobs(tmp_path)] == before
    assert [method for method, _, _ in session.requests] == ['GET']


@pytest.mark.parametrize('translator,model', [('ollama', 'bad model'), ('libretranslate', 'UPPER')])
def test_test_translator_validates_model_before_http(tmp_path, monkeypatch, translator, model):
    config(tmp_path)
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['test-translator', '--translator', translator, '--model', model])
    assert result.exit_code == 1 and 'invalid_provider_model' in text(result), text(result)
    assert session.requests == []


def test_unknown_translator_with_explicit_model_keeps_unsupported_error(tmp_path, monkeypatch):
    config(tmp_path)
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['test-translator', '--translator', 'unknown', '--model', 'label'])
    assert result.exit_code == 1 and 'unsupported_translator' in text(result), text(result)
    assert session.requests == []


def test_explicit_test_model_ignores_an_invalid_configured_model(tmp_path, monkeypatch):
    path = config(tmp_path, label=None, translator_models={'ollama': 'bad model'})
    before = path.read_bytes()
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, ['test-translator', '--translator', 'ollama', '--model', MODEL])
    assert result.exit_code == 0, text(result)
    assert 'Translator OK:' in text(result)
    assert any(body['model'] == MODEL for method, _, body in session.requests if method == 'POST')
    assert path.read_bytes() == before
