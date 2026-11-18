"""Guided settings: the owner declares, changes or removes the translator model label."""
import json

import pytest

from ayvu.interfaces.cli.app import app
from tests.support.cli_support import isolated_config, runner  # noqa: F401


@pytest.fixture(autouse=True)
def no_processing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")


def saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def with_label(path, label):
    data = saved(path)
    data["translator_models"] = {"libretranslate": label}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_settings_show_no_label_and_offer_the_option(isolated_config):
    result = runner.invoke(app, [], input="4\n0\n")
    assert result.exit_code == 0
    row = next(line for line in result.output.splitlines() if "Translator model label" in line and "│" in line)
    assert row.split("│")[2].strip() == "-"
    assert "5. Change translator model label" in result.output


def test_declaring_a_label_persists_it(isolated_config):
    result = runner.invoke(app, [], input="4\n5\nlocal-model-2026\n0\n")
    assert result.exit_code == 0, result.output
    assert "Settings saved." in result.output
    assert saved(isolated_config)["translator_models"] == {"libretranslate": "local-model-2026"}
    assert "local-model-2026" in result.output.split("Settings saved.")[-1]


@pytest.mark.parametrize("label", ["Bad Label", "UPPER", "-starts-with-dash", "x" * 49, "ünicode"])
def test_an_invalid_label_is_refused_and_nothing_is_saved(isolated_config, label):
    before = isolated_config.read_bytes()
    result = runner.invoke(app, [], input=f"4\n5\n{label}\n0\n")
    assert result.exit_code == 0, result.output
    assert "Rótulo de modelo inválido." in result.output
    assert "Settings saved." not in result.output
    assert isolated_config.read_bytes() == before


def test_enter_keeps_the_current_label(isolated_config):
    with_label(isolated_config, "local-model")
    before = isolated_config.read_bytes()
    result = runner.invoke(app, [], input="4\n5\n\n0\n")
    assert result.exit_code == 0, result.output
    assert "Settings unchanged." in result.output
    assert isolated_config.read_bytes() == before


def test_dash_removes_the_label_and_explains_the_effect(isolated_config):
    with_label(isolated_config, "local-model")
    result = runner.invoke(app, [], input="4\n5\n-\n0\n")
    assert result.exit_code == 0, result.output
    assert "Settings saved." in result.output
    assert "Without a model label, new translations are refused." in result.output
    assert "Resuming a rich job with another label translates its documents again." not in result.output
    assert "translator_models" not in saved(isolated_config)


def test_changing_the_label_keeps_resume_bound_to_its_original_model(isolated_config):
    with_label(isolated_config, "first-model")
    result = runner.invoke(app, [], input="4\n5\nsecond-model\n0\n")
    assert result.exit_code == 0, result.output
    assert "Resuming a rich job with another label translates its documents again." not in result.output
    assert saved(isolated_config)["translator_models"] == {"libretranslate": "second-model"}


@pytest.mark.parametrize("answer,expected", [("second-model", {"libretranslate": "second-model"}), ("-", {})])
def test_editing_the_label_keeps_the_ollama_model(isolated_config, answer, expected):
    data = saved(isolated_config)
    data["translator_models"] = {"libretranslate": "first-model", "ollama": "qwen2.5:7b"}
    isolated_config.write_text(json.dumps(data), encoding="utf-8")
    result = runner.invoke(app, [], input=f"4\n5\n{answer}\n0\n")
    assert result.exit_code == 0, result.output
    assert saved(isolated_config)["translator_models"] == {**expected, "ollama": "qwen2.5:7b"}


def test_removing_when_there_is_no_label_changes_nothing(isolated_config):
    before = isolated_config.read_bytes()
    result = runner.invoke(app, [], input="4\n5\n-\n0\n")
    assert result.exit_code == 0, result.output
    assert "Settings unchanged." in result.output
    assert isolated_config.read_bytes() == before
