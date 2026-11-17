"""Every new translation surface refuses an undeclared model before any side effect."""
import pytest
import requests
from typer.testing import CliRunner

from ayvu.interfaces.cli.app import app
from tests.support.cli_translate_project_support import _book
from tests.support.model_label_support import assert_refused, snapshot


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def no_http(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("model-label admission crossed the HTTP transport")
    monkeypatch.setattr(requests.Session, "request", request)
    return calls


@pytest.mark.parametrize("surface", ["translate", "batch", "preview", "dry-run", "cache-only"])
@pytest.mark.parametrize("saved_config", [False, True], ids=["first-use", "configured"])
def test_new_translation_requires_a_model_without_creating_anything(tmp_path, no_http, surface, saved_config):
    source = _book(tmp_path / "book.epub")
    if saved_config:
        from tests.support.cli_rich_runtime_support import config
        config(tmp_path, label=None)
    args = ["--mode", "developer"]
    if surface == "preview":
        args += ["--preview", str(source)]
    else:
        args += ["translate", str(source)]
        if surface == "batch":
            args += [str(_book(tmp_path / "second.epub"))]
        args += ["--source", "en", "--target", "pt"]
        if surface in ("dry-run", "cache-only"):
            args += ["--" + surface]
    before = snapshot(tmp_path)
    result = CliRunner().invoke(app, args)
    assert_refused(result)
    assert snapshot(tmp_path) == before
    assert no_http == []


@pytest.mark.parametrize("menu", ["1", "2"], ids=["translation", "preview"])
def test_guided_enter_cancels_without_creating_anything(tmp_path, no_http, menu):
    from tests.support.cli_rich_runtime_support import config
    config(tmp_path, label=None)
    before = snapshot(tmp_path)
    result = CliRunner().invoke(app, [], input=f"{menu}\n\n")
    assert_refused(result)
    assert "Translator model label" in result.output and "Canceled." in result.output
    assert snapshot(tmp_path) == before
    assert no_http == []


@pytest.mark.parametrize("arguments", [(), ("--dry-run",), ("--cache-only",)])
def test_unknown_translator_has_priority_without_a_label(tmp_path, no_http, arguments):
    source = _book(tmp_path / "book.epub")
    before = snapshot(tmp_path)
    result = CliRunner().invoke(app, ["--mode", "developer", "translate", str(source),
        "--translator", "unknown", *arguments])
    assert result.exit_code == 1
    assert "Unsupported translator: unknown." in " ".join(result.output.split())
    assert "declare your translator" not in result.output
    assert snapshot(tmp_path) == before and no_http == []
