"""Guided glossary behavior through the CLI, with a private local configuration."""

import json

import pytest
from typer.testing import CliRunner

import ayvu.cli as cli_module
from ayvu.cli import app


@pytest.fixture
def local_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.cli.default_processing_dir", lambda: tmp_path / "missing")
    config = tmp_path / "config" / "ayvu" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": 1, "default_target_language": "pt"}))
    # The existing default configuration may deliberately ignore XDG_CONFIG_HOME.
    monkeypatch.setattr("ayvu.cli.default_glossaries_dir", lambda: tmp_path / "glossaries")
    return tmp_path / "glossaries"


def run(input_text):
    return CliRunner().invoke(app, [], input=input_text)


def test_create_normalizes_name_and_updates_case_insensitively(local_catalog):
    result = run("5\n1\nTechnical Terms.json\nStraße\n1\nprimeiro\ny\nSTRASSE\n1\nsegundo\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert "Term updated:" in result.output
    assert "Glossary saved:" in result.output
    assert json.loads((local_catalog / "technical-terms.json").read_text()) == {
        "STRASSE": {"rule": "translate", "translation": "segundo"}
    }


def test_empty_create_does_not_save(local_catalog):
    result = run("5\n1\nEmpty\n\n")
    assert result.exit_code == 0, result.output
    assert "Glossary was not saved because it has no terms." in result.output
    assert not list(local_catalog.glob("*.json"))


def test_edit_preserves_untouched_advanced_fields_and_preview(local_catalog):
    local_catalog.mkdir()
    path = local_catalog / "advanced.json"
    path.write_text(json.dumps({"A": {"rule": "translate", "translation": "Um", "required": True},
                                "B": {"rule": "preserve", "required": True},
                                "C": {"rule": "forbidden"}}))
    result = run("5\n2\n1\nD\n2\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert "Glossary preview" in result.output
    assert json.loads(path.read_text()) == {
        "A": {"rule": "translate", "translation": "Um", "required": True},
        "B": {"rule": "preserve", "required": True},
        "C": {"rule": "forbidden"}, "D": {"rule": "preserve"}}
    preview = run("5\n3\n1\n")
    assert preview.exit_code == 0, preview.output
    assert "Glossary preview" in preview.output
    assert "Um" in preview.output


def test_existing_name_prompts_and_never_overwrites(local_catalog):
    local_catalog.mkdir()
    path = local_catalog / "existing.json"
    path.write_text('{"old": "value"}')
    before = path.read_bytes()
    result = run("5\n1\nExisting\nn\n")
    assert result.exit_code == 0, result.output
    assert "Glossary already exists:" in result.output
    assert "Glossary was not created." in result.output
    assert path.read_bytes() == before


def test_translation_invalid_selection_allows_another(local_catalog, tmp_path, monkeypatch):
    local_catalog.mkdir()
    (local_catalog / "a-invalid.json").write_text("{broken")
    valid = local_catalog / "b-valid.json"
    valid.write_text('{"term": "translation"}')
    book = tmp_path / "book.epub"
    book.write_bytes(b"fake")
    result = run(f"1\n{book}\n1\n1\n2\ny\n")
    assert "Choose another glossary or run without glossary." in result.output
    assert "Glossary selected:" in result.output
    assert str(valid) in result.output


def _glossaries_dir(tmp_path):
    from ayvu.cli import default_glossaries_dir

    directory = default_glossaries_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_refused_glossaries_are_described_in_words(local_catalog, tmp_path):
    directory = _glossaries_dir(tmp_path)
    (directory / "broken.json").write_text("{nope", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text('{"X": "Y"}', encoding="utf-8")
    (directory / "link.json").symlink_to(outside)

    result = CliRunner().invoke(app, [], input="5\n3\n1\n0\n")

    flat = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "invalid file" in flat and "link, not accepted" in flat
    assert "the file is not a valid glossary." in flat
    assert "unsafe-link" not in flat


def test_an_edit_of_a_file_changed_after_it_was_opened_is_refused_and_says_what_to_do(local_catalog, tmp_path, monkeypatch):
    directory = _glossaries_dir(tmp_path)
    path = directory / "tech.json"
    path.write_text('{"Observer": {"rule": "preserve"}}\n', encoding="utf-8")
    real_terms = cli_module._prompt_guided_glossary_terms

    def someone_saves_while_the_person_types(entries):
        path.write_text('{"Observer": {"rule": "preserve"}, "Other": "Outro"}\n', encoding="utf-8")
        return real_terms(entries)

    monkeypatch.setattr(cli_module, "_prompt_guided_glossary_terms", someone_saves_while_the_person_types)

    result = CliRunner().invoke(app, [], input="5\n2\n1\nNew\n1\nnovo\nn\ny\n")

    flat = " ".join(result.output.split())
    assert "Glossary was not saved" in flat and "Open it again" in flat
    assert "Other" in path.read_text(encoding="utf-8")
    assert "New" not in path.read_text(encoding="utf-8")
