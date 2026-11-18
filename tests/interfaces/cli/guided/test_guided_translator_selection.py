"""Settings discover real adapter model names over fake HTTP and preserve canceled choices (#377)."""
import json

import pytest

from ayvu.interfaces.cli.app import app
from tests.support.cli_rich_runtime_support import config, isolate, runner, text  # noqa: F401
from tests.support.ollama_selection_support import OllamaTransport
from tests.support.ollama_fakes import MODEL


@pytest.mark.parametrize('unavailable,answer', [(False, '2'), (True, MODEL)])
def test_settings_select_discovered_or_manual_ollama_model(tmp_path, monkeypatch, unavailable, answer):
    path = config(tmp_path)
    session = OllamaTransport(unavailable=unavailable)
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, [], input=f'4\n6\nollama\n{answer}\n0\n')
    assert result.exit_code == 0, text(result)
    assert 'Settings saved.' in text(result)
    assert json.loads(path.read_text())['translator_models'] == {'libretranslate': 'local-model-2026', 'ollama': MODEL}
    assert session.requests == [('GET', 'http://localhost:11434/api/tags', None)]
    assert ('Model discovery failed' in text(result)) is unavailable
    if not unavailable:
        assert f'2. {MODEL}' in text(result)


@pytest.mark.parametrize('answers', ['\n', 'ollama\n\n', 'ollama\nbad model\n', 'libretranslate\nUPPER\n',
                                      'unknown\n'])
def test_canceled_or_invalid_settings_leave_bytes_unchanged(tmp_path, monkeypatch, answers):
    path = config(tmp_path)
    before = path.read_bytes()
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, [], input=f'4\n6\n{answers}0\n')
    assert result.exit_code == 0, text(result)
    assert 'Translator (libretranslate or ollama; Enter cancels)' in text(result)
    assert path.read_bytes() == before and 'Settings saved.' not in text(result)


def test_settings_choose_a_libretranslate_label(tmp_path):
    path = config(tmp_path, label=None)
    result = runner.invoke(app, [], input='4\n6\nlibretranslate\nchosen-model\n0\n')
    assert result.exit_code == 0, text(result)
    assert json.loads(path.read_text())['translator_models'] == {'libretranslate': 'chosen-model'}


def test_guided_translation_uses_an_ollama_profile_without_a_libretranslate_label(tmp_path, monkeypatch):
    from tests.support.cli_translate_project_support import _book, _jobs
    from tests.support.cli_rich_runtime_support import records

    config(tmp_path, label=None, profiles={'local': {'translator': 'ollama', 'model': MODEL}})
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, [], input=f'1\n{source}\n1\n\n1\ny\nn\n')
    assert result.exit_code == 0, text(result)
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == 'translate_project')
    assert {record.producer.provider for record in records(tmp_path, job)} == {'ollama'}
    assert all('/api/' in url for _, url, _ in session.requests)


def test_guided_translation_discovers_and_saves_a_missing_ollama_model(tmp_path, monkeypatch):
    from tests.support.cli_translate_project_support import _book

    path = config(tmp_path, label=None, profiles={'local': {'translator': 'ollama'}})
    source = _book(tmp_path / 'book.epub')
    session = OllamaTransport()
    monkeypatch.setattr('requests.Session', lambda: session)
    result = runner.invoke(app, [], input=f'1\n{source}\n1\n\n2\n1\ny\nn\n')
    assert result.exit_code == 0, text(result)
    assert json.loads(path.read_text())['translator_models'] == {'ollama': MODEL}
    assert f'2. {MODEL}' in text(result) and 'Settings saved.' in text(result)


def test_with_libretranslate_only_guided_translation_asks_nothing_new(tmp_path, monkeypatch):
    from tests.interfaces.cli.test_cli_rich_surfaces import guided

    config(tmp_path)
    result, session = guided(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    assert 'Translator (' not in text(result)
    assert any(method == 'POST' and url.startswith('http://localhost:5000') for method, url, _ in session.requests)


def test_guided_preview_stays_on_libretranslate_even_with_an_ollama_model(tmp_path, monkeypatch):
    from tests.support.cli_rich_runtime_support import Transport
    from tests.support.cli_translate_project_support import _book

    config(tmp_path, label=None, translator_models={'libretranslate': 'local-model-2026', 'ollama': MODEL})
    source = _book(tmp_path / 'book.epub')
    session = Transport()
    monkeypatch.setattr('ayvu.providers.libretranslate.client.requests.Session', lambda: session)
    result = runner.invoke(app, [], input=f'2\n{source}\n1\n')
    assert result.exit_code == 0, text(result)
    assert 'Translator (' not in text(result)
    assert session.requests and all(url.startswith('http://localhost:5000') for _, url, _ in session.requests)
