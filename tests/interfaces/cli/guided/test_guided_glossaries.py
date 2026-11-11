import json
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import isolated_config, runner


def test_root_command_creates_guided_glossary(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="5\n1\nTechnical Terms\nGame Loop\n1\nloop de jogo\ny\nObserver\n2\nn\ny\n",
    )

    glossary_path = isolated_config.parent / "glossaries" / "technical-terms.json"
    assert result.exit_code == 0
    assert "Glossaries" in result.output
    assert "Glossary preview" in result.output
    assert "Glossary saved:" in result.output
    assert json.loads(glossary_path.read_text(encoding="utf-8")) == {
        "Game Loop": {"rule": "translate", "translation": "loop de jogo"},
        "Observer": {"rule": "preserve"},
    }


def test_root_command_edits_guided_glossary(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    glossary_dir = isolated_config.parent / "glossaries"
    glossary_dir.mkdir()
    glossary_path = glossary_dir / "technical.json"
    glossary_path.write_text('{"Observer": {"rule": "preserve"}}\n', encoding="utf-8")

    result = runner.invoke(
        app,
        [],
        input="5\n2\n1\nObject Pool\n1\npool de objetos\nn\ny\n",
    )

    assert result.exit_code == 0
    assert "Edit saved glossary" in result.output
    assert "Saved glossaries" in result.output
    assert "Term added:" in result.output
    assert json.loads(glossary_path.read_text(encoding="utf-8")) == {
        "Observer": {"rule": "preserve"},
        "Object Pool": {"rule": "translate", "translation": "pool de objetos"},
    }
