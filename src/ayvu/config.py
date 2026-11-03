from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .preferences import GlobalPreferences, NamedPreferenceProfile, PreferenceFolders, PreferencesError, preferences_from_mapping
from .preferences_service import (
    PreferencesSnapshot, UpdatePreferences, UpdatePreferencesResult,
    get_preferences, update_preferences,
)
from .preferences_store import JsonPreferencesRepository


CONFIG_VERSION = 1
DEFAULT_TARGET_LANGUAGE = "pt"
DEFAULT_BOOKS_DIR = "~/Documentos/Livros"
DEFAULT_CONFIG_DIR = "ayvu"
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_GLOSSARIES_DIR = "glossaries"
PROFILE_STYLE_NEUTRAL = "neutral"
PROFILE_STYLE_TECHNICAL = "technical"
PROFILE_STYLE_LITERARY = "literary"
PROFILE_STYLE_ACADEMIC = "academic"
PROFILE_STYLES = {
    PROFILE_STYLE_NEUTRAL,
    PROFILE_STYLE_TECHNICAL,
    PROFILE_STYLE_LITERARY,
    PROFILE_STYLE_ACADEMIC,
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class FolderNames:
    original: str = "Original"
    translated: str = "Traduzidos"
    preview: str = "Preview"
    reports: str = "Relatorios"
    processing: str = "Processando"

    @classmethod
    def from_dict(cls, data: object) -> "FolderNames":
        try:
            value, _ = preferences_from_mapping({'folders': data})
            return cls(**{key: getattr(value.folders, key) for key in cls.__dataclass_fields__})
        except PreferencesError as exc:
            raise ConfigError(_compatibility_error(exc)) from None

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, repr=False)
class TranslationProfile:
    target_language: str | None = None
    glossary: Path | None = None
    style: str | None = None

    @classmethod
    def from_dict(cls, data: object, profile_name: str) -> "TranslationProfile":
        try:
            value, _ = preferences_from_mapping({'profiles': {'profile': data}})
            profile = value.profiles[0][1]
            return cls(profile.target_language, Path(profile.glossary) if profile.glossary is not None else None,
                       profile.style)
        except PreferencesError as exc:
            raise ConfigError(_compatibility_error(exc)) from None

    def to_dict(self) -> dict[str, str]:
        data: dict[str, str] = {}
        if self.target_language is not None:
            data["target_language"] = self.target_language
        if self.glossary is not None:
            data["glossary"] = str(self.glossary)
        if self.style is not None:
            data["style"] = self.style
        return data


@dataclass(frozen=True, repr=False)
class AyvuConfig:
    version: int = CONFIG_VERSION
    default_target_language: str = DEFAULT_TARGET_LANGUAGE
    books_dir: Path = Path(DEFAULT_BOOKS_DIR)
    folders: FolderNames = field(default_factory=FolderNames)
    reader_app: str | None = None
    profiles: dict[str, TranslationProfile] = field(default_factory=dict)
    _preferences_revision: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def default(cls) -> "AyvuConfig":
        return cls()

    @classmethod
    def from_dict(cls, data: object) -> "AyvuConfig":
        try:
            value, _ = preferences_from_mapping(data)
            return config_from_preferences(value)
        except PreferencesError as exc:
            raise ConfigError(_compatibility_error(exc)) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "default_target_language": self.default_target_language,
            "books_dir": str(self.books_dir),
            "folders": self.folders.to_dict(),
            "reader_app": self.reader_app,
            "profiles": {
                name: profile.to_dict()
                for name, profile in sorted(self.profiles.items())
            },
        }

    @property
    def resolved_books_dir(self) -> Path:
        return self.books_dir.expanduser()

    @property
    def original_dir(self) -> Path:
        return self.resolved_books_dir / self.folders.original

    @property
    def translated_dir(self) -> Path:
        return self.resolved_books_dir / self.folders.translated

    @property
    def preview_dir(self) -> Path:
        return self.resolved_books_dir / self.folders.preview

    @property
    def reports_dir(self) -> Path:
        return self.resolved_books_dir / self.folders.reports

    @property
    def processing_dir(self) -> Path:
        return self.resolved_books_dir / self.folders.processing


@dataclass
class ConfigStore:
    path: Path = field(repr=False)
    _snapshot: PreferencesSnapshot | None = field(default=None, init=False, repr=False)

    @classmethod
    def default(cls) -> "ConfigStore":
        return cls(default_config_path())

    def get_preferences(self) -> PreferencesSnapshot:
        self._snapshot = get_preferences(JsonPreferencesRepository(self.path))
        return self._snapshot

    def load(self) -> AyvuConfig:
        snapshot = self.get_preferences()
        if snapshot.error:
            raise ConfigError(_compatibility_error(snapshot.error)) from None
        return config_from_preferences(snapshot.preferences, snapshot.revision)

    def update_preferences(self, config: AyvuConfig, expected_revision: str) -> UpdatePreferencesResult:
        try:
            preferences = config_to_preferences(config)
        except PreferencesError as exc:
            raise ConfigError(_compatibility_error(exc)) from None
        result = update_preferences(JsonPreferencesRepository(self.path),
                                    UpdatePreferences(preferences, expected_revision))
        if result.persisted:
            self._snapshot = result.snapshot
        return result

    def save(self, config: AyvuConfig) -> Path:
        revision = config._preferences_revision
        if revision is None:
            revision = self._snapshot.revision if self._snapshot is not None else 'missing'
        result = self.update_preferences(config, revision)
        if not result.persisted:
            raise ConfigError(_compatibility_error(result.error)) from None
        return self.path


def config_to_preferences(config: AyvuConfig) -> GlobalPreferences:
    return GlobalPreferences(
        version=config.version, default_target_language=config.default_target_language,
        books_dir=str(config.books_dir),
        folders=PreferenceFolders(**config.folders.to_dict()), reader_app=config.reader_app,
        profiles=tuple((name, NamedPreferenceProfile(
            profile.target_language, str(profile.glossary) if profile.glossary is not None else None,
            profile.style)) for name, profile in sorted(config.profiles.items())),
    )


def config_from_preferences(value: GlobalPreferences, revision: str | None = None) -> AyvuConfig:
    return AyvuConfig(
        version=value.version, default_target_language=value.default_target_language,
        books_dir=Path(value.books_dir), folders=FolderNames(**{
            key: getattr(value.folders, key) for key in FolderNames.__dataclass_fields__}),
        reader_app=value.reader_app,
        profiles={name: TranslationProfile(profile.target_language,
                  Path(profile.glossary) if profile.glossary is not None else None, profile.style)
                  for name, profile in value.profiles}, _preferences_revision=revision,
    )


def _compatibility_error(error: PreferencesError) -> str:
    if error.code == 'malformed':
        return 'Config file is not valid JSON.'
    if error.code == 'unsupported_version':
        return 'Unsupported config version.'
    if error.code == 'invalid' and error.field == 'profiles':
        return 'Config field profiles must be an object with valid profile fields.'
    if error.code == 'invalid' and error.field == 'profiles.*.style':
        return 'Config profile field style must be one of: academic, literary, neutral, technical.'
    if error.code == 'invalid' and error.field.startswith('folders.'):
        return 'Config folder name must be a folder name, not a path.'
    return f'Config operation failed ({error.code}, {error.field}).'


def default_config_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    environment = os.environ if env is None else env
    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        base_dir = Path(config_home).expanduser()
    else:
        base_dir = (home or Path.home()) / ".config"
    return base_dir / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILE


def default_glossaries_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    return default_config_path(env=env, home=home).parent / DEFAULT_GLOSSARIES_DIR
