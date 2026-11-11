import json
from ayvu.interfaces.cli.app import app
from test_cli import FakeNoLanguagesTranslator, isolated_config, runner


def test_first_use_failed_save_retains_session_without_claiming_success(isolated_config, tmp_path, monkeypatch, minimal_epub_path):
    isolated_config.unlink()
    original_dir = tmp_path / 'Books' / 'Original'
    original_dir.mkdir(parents=True)
    (original_dir / 'first-use.epub').write_bytes(minimal_epub_path.read_bytes())
    monkeypatch.setattr('ayvu.composition.builders.providers.LibreTranslateTranslator', FakeNoLanguagesTranslator)
    def fail(*args):
        raise PermissionError('private-canary')
    monkeypatch.setattr('ayvu.preferences.store.os.replace', fail)
    result = runner.invoke(app, [], input=f'es\n{tmp_path / "Books"}\ny\n3\n0\n')
    assert result.exit_code == 0
    assert 'não foi persistida' in result.output
    assert 'Idioma padrão salvo:' not in result.output
    assert 'first-use' in result.output
    assert not isolated_config.exists()
    assert 'private-canary' not in result.output


def test_root_command_first_use_asks_and_saves_default_language(isolated_config, tmp_path, monkeypatch):
    isolated_config.unlink()
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(app, [], input=f"es\n{books_dir}\ny\n0\n")

    assert result.exit_code == 0
    assert "Primeiro uso do modo comum." in result.output
    assert "Idioma padrão salvo:" in result.output
    assert "Pasta base salva:" in result.output
    assert "Manter estes nomes de pastas?" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_target_language"] == "es"
    assert saved["books_dir"] == str(books_dir)
    assert saved["folders"] == {
        "original": "Original",
        "translated": "Traduzidos",
        "preview": "Preview",
        "reports": "Relatorios",
        "processing": "Processando",
    }


def test_root_command_first_use_can_change_feature_folder_names(isolated_config, tmp_path, monkeypatch):
    isolated_config.unlink()
    books_dir = tmp_path / "Minha Biblioteca"
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FakeNoLanguagesTranslator)

    result = runner.invoke(
        app,
        [],
        input=f"es\n{books_dir}\nn\nOriginais\nPT\nAmostras\nRelatorios-MD\nEm-Andamento\n0\n",
    )

    assert result.exit_code == 0
    assert "Feature folders" in result.output
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["folders"] == {
        "original": "Originais",
        "translated": "PT",
        "preview": "Amostras",
        "reports": "Relatorios-MD",
        "processing": "Em-Andamento",
    }


def test_root_command_does_not_reask_when_config_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Primeiro uso do modo comum." not in result.output
    assert "Manter estes nomes de pastas?" not in result.output
    assert "Choose an option" in result.output


def test_root_command_ignores_invalid_config(isolated_config, tmp_path, monkeypatch):
    isolated_config.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="0\n")

    assert result.exit_code == 0
    assert "Configuração inválida ignorada:" in result.output
    assert "Choose an option" in result.output


def test_root_command_can_show_help_from_guided_menu(tmp_path, monkeypatch):
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, [], input="6\n")

    assert result.exit_code == 0
    assert "Show command help" in result.output
    assert "Usage:" in result.output
    assert "translate" in result.output
