import json
from ayvu.interfaces.cli.app import app
from test_cli import FakeNoLanguagesTranslator, isolated_config, runner


def test_root_command_settings_keeps_language_when_not_changed(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="4\n1\npt\n0\n")

    assert result.exit_code == 0
    assert "Settings" in result.output
    assert "Default target language" in result.output
    assert "Change default language" in result.output
    assert "Settings unchanged." in result.output
    assert "Settings closed." in result.output


def test_root_command_settings_changes_and_persists_language(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(app, [], input="4\n1\nes\n0\n")

    assert result.exit_code == 0
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "es"


def test_settings_unknown_fields_warn_and_refuse_lossy_update(isolated_config, monkeypatch):
    payload = b'{"version":1,"private_extension":{"value":"secret-canary"}}'
    isolated_config.write_bytes(payload)
    monkeypatch.setattr('ayvu.composition.builders.providers.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    result = runner.invoke(app, [], input='4\n1\nes\n0\n')
    assert result.exit_code == 0
    assert 'somente leitura' in result.output
    assert 'Settings saved.' not in result.output
    assert 'não foi persistida' in result.output
    assert 'secret-canary' not in result.output
    assert isolated_config.read_bytes() == payload


def test_settings_save_failure_keeps_effective_session_values(isolated_config, monkeypatch):
    original = isolated_config.read_bytes()
    monkeypatch.setattr('ayvu.composition.builders.providers.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    def fail(*args):
        raise OSError('private-canary-path')
    monkeypatch.setattr('ayvu.preferences.store.os.replace', fail)
    result = runner.invoke(app, [], input='4\n1\nes\n0\n')
    assert result.exit_code == 0
    assert 'Settings saved.' not in result.output
    assert 'não foi persistida' in result.output
    assert 'es' in result.output.split('não foi persistida.')[-1]
    assert 'private-canary' not in result.output
    assert isolated_config.read_bytes() == original


def test_settings_multiple_saves_advance_the_expected_revision(isolated_config, monkeypatch):
    monkeypatch.setattr('ayvu.composition.builders.providers.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    result = runner.invoke(app, [], input='4\n1\nes\n4\nfoliate\n0\n')
    assert result.exit_code == 0
    assert result.output.count('Settings saved.') == 2
    saved = json.loads(isolated_config.read_text())
    assert saved['default_target_language'] == 'es'
    assert saved['reader_app'] == 'foliate'


def test_root_command_settings_changes_books_dir(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input=f"4\n2\n{books_dir}\n0\n")

    assert result.exit_code == 0
    assert "Books folder" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["books_dir"] == str(books_dir)


def test_root_command_settings_changes_feature_folder_names(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="4\n3\nOriginais\nPT\nAmostras\nRelatorios-MD\nEm-Andamento\n0\n",
    )

    assert result.exit_code == 0
    assert "Change feature folder names" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["folders"] == {
        "original": "Originais",
        "translated": "PT",
        "preview": "Amostras",
        "reports": "Relatorios-MD",
        "processing": "Em-Andamento",
    }


def test_root_command_settings_rejects_folder_paths(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(
        app,
        [],
        input="4\n3\nOriginais\nLivros/PT\nAmostras\nRelatorios\nProcessando\n0\n",
    )

    assert result.exit_code == 0
    assert "Invalid folder name:" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "pt"
    assert "folders" not in saved


def test_root_command_settings_changes_reader_app(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="4\n4\nfoliate\n0\n")

    assert result.exit_code == 0
    assert "Reader app" in result.output
    assert "Settings saved." in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["reader_app"] == "foliate"
