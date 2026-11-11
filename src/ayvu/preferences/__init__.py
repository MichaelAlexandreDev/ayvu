"""Global preferences: the version-1 values and the repository port. The JSON store and the CLI facade are the
adapter modules preferences.store and preferences.config_facade.
"""
from __future__ import annotations

from .model import (
    AyvuConfig, ConfigError, DEFAULT_BOOKS_DIR, FolderNames, GlobalPreferences, NamedPreferenceProfile,
    PreferencesError, TranslationProfile, config_from_preferences,
)
from .service import PreferencesRepository, PreferencesSnapshot, UpdatePreferencesResult, get_preferences

__all__ = [
    "AyvuConfig", "ConfigError", "DEFAULT_BOOKS_DIR", "FolderNames", "GlobalPreferences",
    "NamedPreferenceProfile", "PreferencesError", "PreferencesRepository", "PreferencesSnapshot",
    "TranslationProfile", "UpdatePreferencesResult", "config_from_preferences", "get_preferences",
]
