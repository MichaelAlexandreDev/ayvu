from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .preferences import PreferencesError
from .preferences_service import (
    PreferencesSnapshot, UpdatePreferences, UpdatePreferencesResult,
    get_preferences, update_preferences,
)
from .preferences_store import JsonPreferencesRepository


from .config_values import (
    CONFIG_VERSION, DEFAULT_TARGET_LANGUAGE, DEFAULT_BOOKS_DIR, DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE, DEFAULT_GLOSSARIES_DIR, PROFILE_STYLE_NEUTRAL,
    PROFILE_STYLE_TECHNICAL, PROFILE_STYLE_LITERARY, PROFILE_STYLE_ACADEMIC, PROFILE_STYLES,
    ConfigError, FolderNames, TranslationProfile, AyvuConfig, config_to_preferences,
    config_from_preferences, _compatibility_error,
)

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
