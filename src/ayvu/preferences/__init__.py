"""Global preferences: the version-1 values, the repository port and the JSON store behind the CLI facade."""
from __future__ import annotations

from .config_facade import ConfigStore, default_glossaries_dir
from .model import (
    AyvuConfig, ConfigError, DEFAULT_BOOKS_DIR, FolderNames, GlobalPreferences, NamedPreferenceProfile,
    PreferencesError, TranslationProfile, config_from_preferences,
)
from .service import PreferencesRepository, PreferencesSnapshot, UpdatePreferencesResult, get_preferences

__all__ = [
    "AyvuConfig", "ConfigError", "ConfigStore", "DEFAULT_BOOKS_DIR", "FolderNames", "GlobalPreferences",
    "NamedPreferenceProfile", "PreferencesError", "PreferencesRepository", "PreferencesSnapshot",
    "TranslationProfile", "UpdatePreferencesResult", "config_from_preferences", "default_glossaries_dir",
    "get_preferences",
]
