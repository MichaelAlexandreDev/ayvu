import json
from pathlib import Path

import pytest

from ayvu.config import (
    AyvuConfig,
    ConfigError,
    ConfigStore,
    FolderNames,
    TranslationProfile,
    default_config_path,
    default_glossaries_dir,
)


def test_default_config_path_uses_xdg_config_home():
    path = default_config_path({"XDG_CONFIG_HOME": "/tmp/custom-config"})

    assert path == Path("/tmp/custom-config/ayvu/config.json")


def test_default_config_path_falls_back_to_home_config(tmp_path):
    path = default_config_path({}, home=tmp_path)

    assert path == tmp_path / ".config" / "ayvu" / "config.json"


def test_default_glossaries_dir_uses_ayvu_config_folder():
    path = default_glossaries_dir({"XDG_CONFIG_HOME": "/tmp/custom-config"})

    assert path == Path("/tmp/custom-config/ayvu/glossaries")


def test_default_config_defines_initial_preferences():
    config = AyvuConfig.default()

    assert config.version == 1
    assert config.default_target_language == "pt"
    assert config.books_dir == Path("~/Documentos/Livros")
    assert config.folders == FolderNames()
    assert config.reader_app is None
    assert config.profiles == {}


def test_config_serializes_full_initial_format():
    config = AyvuConfig.default()

    assert config.to_dict() == {
        "version": 1,
        "default_target_language": "pt",
        "books_dir": "~/Documentos/Livros",
        "folders": {
            "original": "Original",
            "translated": "Traduzidos",
            "preview": "Preview",
            "reports": "Relatorios",
            "processing": "Processando",
        },
        "reader_app": None,
        "profiles": {},
    }


def test_config_loads_partial_file_with_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_target_language": "es"}\n', encoding="utf-8")

    config = ConfigStore(config_path).load()

    assert config.default_target_language == "es"
    assert config.books_dir == Path("~/Documentos/Livros")
    assert config.folders.translated == "Traduzidos"
    assert config.profiles == {}


def test_config_loads_translation_profiles(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "technical": {
                        "target_language": "pt-BR",
                        "glossary": "technical.json",
                        "style": "technical",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    config = ConfigStore(config_path).load()

    assert config.profiles == {
        "technical": TranslationProfile(
            target_language="pt-BR",
            glossary=Path("technical.json"),
            style="technical",
        )
    }
    assert config.to_dict()["profiles"] == {
        "technical": {
            "target_language": "pt-BR",
            "glossary": "technical.json",
            "style": "technical",
        }
    }


def test_config_resolves_feature_directories(tmp_path):
    books_dir = tmp_path / "Biblioteca"
    config = AyvuConfig(
        books_dir=books_dir,
        folders=FolderNames(
            original="Originais",
            translated="PT",
            preview="Amostras",
            reports="Relatorios",
            processing="Em-Andamento",
        ),
    )

    assert config.original_dir == books_dir / "Originais"
    assert config.translated_dir == books_dir / "PT"
    assert config.preview_dir == books_dir / "Amostras"
    assert config.reports_dir == books_dir / "Relatorios"
    assert config.processing_dir == books_dir / "Em-Andamento"


def test_config_store_saves_json_with_parent_directories(tmp_path):
    config_path = tmp_path / "nested" / "ayvu" / "config.json"
    config = AyvuConfig(default_target_language="es", reader_app="foliate")

    saved_path = ConfigStore(config_path).save(config)

    assert saved_path == config_path
    assert json.loads(config_path.read_text(encoding="utf-8"))["reader_app"] == "foliate"


def test_missing_config_file_returns_defaults(tmp_path):
    config = ConfigStore(tmp_path / "missing.json").load()

    assert config == AyvuConfig.default()


def test_invalid_json_raises_config_error(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid JSON"):
        ConfigStore(config_path).load()


def test_unsupported_version_raises_config_error(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 99}\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="Unsupported config version"):
        ConfigStore(config_path).load()


def test_folder_names_must_not_be_paths():
    with pytest.raises(ConfigError, match="must be a folder name"):
        FolderNames.from_dict({"translated": "Livros/Traduzidos"})


def test_blank_default_language_is_rejected():
    with pytest.raises(ConfigError, match="default_target_language"):
        AyvuConfig.from_dict({"version": 1, "default_target_language": " "})


def test_legacy_errors_do_not_reflect_private_profile_names():
    with pytest.raises(ConfigError) as caught:
        AyvuConfig.from_dict({'profiles': {'private-canary': {'style': 'invalid'}}})
    assert 'private-canary' not in str(caught.value)


def test_legacy_value_parsing_rejects_credential_commands():
    with pytest.raises(ConfigError) as caught:
        AyvuConfig.from_dict({'reader_app': 'reader --token secret-canary'})
    assert 'secret-canary' not in str(caught.value)


def test_legacy_padded_language_values_preserve_their_meaning(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"version":1,"default_target_language":" pt_BR "}')
    store = ConfigStore(path)
    loaded = store.load()
    assert loaded.default_target_language == ' pt_BR '
    from dataclasses import replace
    store.save(replace(loaded, reader_app='foliate'))
    assert json.loads(path.read_text())['default_target_language'] == ' pt_BR '


def test_translation_profiles_must_be_objects():
    with pytest.raises(ConfigError, match="profiles must be an object"):
        AyvuConfig.from_dict({"version": 1, "profiles": []})


def test_translation_profile_rejects_unknown_style():
    with pytest.raises(ConfigError, match="style must be one of"):
        AyvuConfig.from_dict(
            {
                "version": 1,
                "profiles": {
                    "custom": {
                        "style": "cinematic",
                    }
                },
            }
        )
