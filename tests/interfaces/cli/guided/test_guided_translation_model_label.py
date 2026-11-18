"""Guided translation declares the model in place, then uses the rich runtime."""
import json

import pytest

from ayvu.interfaces.cli.app import app
from tests.support.cli_rich_runtime_support import Transport, config, isolate, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.model_label_support import assert_refused, snapshot


@pytest.mark.parametrize("menu", ["1", "2"], ids=["translation", "preview"])
def test_guided_saves_the_declared_label_and_continues(tmp_path, monkeypatch, menu):
    path = config(tmp_path, label=None)
    source = _book(tmp_path / "book.epub")
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    answers = (f"1\n{source}\n\nlocal-model\n1\ny\nn\n" if menu == "1"
               else f"2\n\nlocal-model\n{source}\n1\n")
    result = runner.invoke(app, [], input=answers)
    assert result.exit_code == 0, text(result)
    assert json.loads(path.read_text())["translator_models"] == {"libretranslate": "local-model"}
    assert "Settings saved." in result.output and transport.posts
    if menu == "1":
        assert all(job.request.command_version == 2 for job in _jobs(tmp_path))
        assert (tmp_path / "books/Traduzidos/book-pt.epub").is_file()
    else:
        assert (tmp_path / "books/Preview/book-preview.epub").is_file()
        assert not (tmp_path / "processing").exists()


def test_guided_label_prompt_keeps_an_existing_ollama_model(tmp_path, monkeypatch):
    path = config(tmp_path, label=None, translator_models={"ollama": "qwen2.5:7b"})
    source = _book(tmp_path / "book.epub")
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    result = runner.invoke(app, [], input=f"2\n\nlocal-model\n{source}\n1\n")
    assert result.exit_code == 0, text(result)
    assert json.loads(path.read_text())["translator_models"] == {"ollama": "qwen2.5:7b", "libretranslate": "local-model"}


@pytest.mark.parametrize("menu", ["1", "2"])
@pytest.mark.parametrize("label", ["Bad Label", "UPPER", "-starts-with-dash", "x" * 49, "ünicode", "-"])
def test_guided_uses_settings_validation_without_saving_invalid_labels(tmp_path, monkeypatch, menu, label):
    config(tmp_path, label=None)
    transport = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: transport)
    source = _book(tmp_path / "book.epub")
    before = snapshot(tmp_path)
    answers = f"1\n{source}\n\n{label}\n" if menu == "1" else f"2\n\n{label}\n"
    result = runner.invoke(app, [], input=answers)
    assert_refused(result)
    assert "Canceled." in result.output
    assert snapshot(tmp_path) == before and transport.requests == []
