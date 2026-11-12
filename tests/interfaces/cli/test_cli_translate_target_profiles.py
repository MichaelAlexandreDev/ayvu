import json
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import _mock_translation_pipeline
from tests.support.cli_support import isolated_config, runner


def test_translate_command_uses_profile_target_and_glossary(
    isolated_config,
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    profile_glossary_path = isolated_config.parent / "glossaries" / "technical.json"
    profile_glossary_path.parent.mkdir(parents=True)
    profile_glossary_path.write_text('{}')
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "technical": {
                        "target_language": "es",
                        "glossary": "technical.json",
                        "style": "technical",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--profile", "technical"],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert "Profile" in result.output
    assert "technical" in result.output
    assert preflight["language_pair"].target == "es"
    assert preflight["glossary_path"] == isolated_config.parent / "glossaries" / "technical.json"
    assert options.target == "es"


def test_translate_command_explicit_target_and_glossary_override_profile(
    isolated_config,
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    profile_glossary_path = isolated_config.parent / "glossaries" / "technical.json"
    explicit_glossary_path = tmp_path / "custom.json"
    explicit_glossary_path.write_text('{}')
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "technical": {
                        "target_language": "es",
                        "glossary": str(profile_glossary_path),
                        "style": "technical",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--profile",
            "technical",
            "--target",
            "fr",
            "--glossary",
            str(explicit_glossary_path),
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["language_pair"].target == "fr"
    assert preflight["glossary_path"] == explicit_glossary_path
    assert options.target == "fr"
