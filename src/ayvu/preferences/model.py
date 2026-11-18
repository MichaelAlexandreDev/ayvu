"""Immutable version-1 preference values, with no filesystem or JSON dependency.

The second half holds the framework-neutral compatibility values the CLI reads (was ``config_values.py``);
neither half constructs a repository.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..provenance import ProvenanceValidationError, declared_model, valid_observed_model_name

FOLDER_KEYS = ('original', 'translated', 'preview', 'reports', 'processing')
TOP_KEYS = ('version', 'default_target_language', 'books_dir', 'folders', 'reader_app', 'profiles', 'translator_models')
PROFILE_KEYS = ('target_language', 'glossary', 'style', 'translator', 'model')
STYLES = frozenset({'neutral', 'technical', 'literary', 'academic'})
FIELD_NAMES = frozenset({*TOP_KEYS, 'configuration', 'revision', 'profiles.*',
                         *(f'folders.{key}' for key in FOLDER_KEYS),
                         *(f'profiles.*.{key}' for key in PROFILE_KEYS)})
ERROR_CODES = frozenset({'invalid', 'malformed', 'oversized', 'unsupported_version',
    'permission', 'io', 'busy', 'unsafe_path', 'unknown_fields', 'conflict',
    'commit_uncertain', 'invalid_revision'})


class PreferencesError(ValueError):
    def __init__(self, code: str, field: str = 'configuration') -> None:
        self.code = code if code in ERROR_CODES else 'io'
        self.field = field if field in FIELD_NAMES else 'configuration'
        super().__init__(f'{self.code}:{self.field}')


def _text(value: object, field: str, limit: int, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise PreferencesError('invalid', field)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise PreferencesError('invalid', field) from None
    # Known credentials/connection syntax has no owner in the v1 preference file.
    credentials = (r'(?:[a-z0-9]+[-_])*'
                   r'(?:api[-_]?key|(?:(?:access|refresh|bearer|auth|id)[-_]?)?token|'
                   r'password|passwd|authorization|secret|credentials?)'
                   r'(?:[-_][a-z0-9]+)*')
    if (re.search(r'(?i)--' + credentials + r'[\"\x27]?(?:\s|=|$)', value)
            or re.search(r'(?i)\b' + credentials + r'\s*[=:]', value)):
        raise PreferencesError('invalid', field)
    if re.search(r'(?i)[a-z][a-z0-9+.-]*://[^/\s]*@', value):
        raise PreferencesError('invalid', field)


def _language(value: str | None, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    _text(value, field, 35)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,34}', value.strip()):
        raise PreferencesError('invalid', field)


@dataclass(frozen=True, repr=False)
class PreferenceFolders:
    original: str = 'Original'
    translated: str = 'Traduzidos'
    preview: str = 'Preview'
    reports: str = 'Relatorios'
    processing: str = 'Processando'

    def __post_init__(self) -> None:
        for key in FOLDER_KEYS:
            value = getattr(self, key)
            _text(value, f'folders.{key}', 128)
            if value in {'.', '..'} or any(char in value for char in '/\\:'):
                raise PreferencesError('invalid', f'folders.{key}')


@dataclass(frozen=True, repr=False)
class NamedPreferenceProfile:
    target_language: str | None = None
    glossary: str | None = None
    style: str | None = None
    translator: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _language(self.target_language, 'profiles.*.target_language', nullable=True)
        if self.translator is not None and (not isinstance(self.translator, str) or self.translator not in MODEL_TRANSLATORS):
            raise PreferencesError('invalid', 'profiles.*.translator')
        if self.model is not None and not MODEL_TRANSLATORS[self.translator or 'libretranslate'](self.model):
            raise PreferencesError('invalid', 'profiles.*.model')
        _text(self.glossary, 'profiles.*.glossary', 4096, nullable=True)
        if self.style is not None and (not isinstance(self.style, str) or self.style not in STYLES):
            raise PreferencesError('invalid', 'profiles.*.style')


@dataclass(frozen=True, repr=False)
class GlobalPreferences:
    version: int = 1
    default_target_language: str = 'pt'
    books_dir: str = '~/Documentos/Livros'
    folders: PreferenceFolders = PreferenceFolders()
    reader_app: str | None = None
    profiles: tuple[tuple[str, NamedPreferenceProfile], ...] = ()
    translator_models: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int:
            raise PreferencesError('invalid', 'version')
        if self.version != 1:
            raise PreferencesError('unsupported_version', 'version')
        _language(self.default_target_language, 'default_target_language')
        _text(self.books_dir, 'books_dir', 4096)
        _text(self.reader_app, 'reader_app', 1024, nullable=True)
        if type(self.folders) is not PreferenceFolders:
            raise PreferencesError('invalid', 'folders')
        if type(self.profiles) is not tuple or len(self.profiles) > 64:
            raise PreferencesError('invalid', 'profiles')
        seen = set()
        for pair in self.profiles:
            if type(pair) is not tuple or len(pair) != 2:
                raise PreferencesError('invalid', 'profiles')
            name, profile = pair
            _text(name, 'profiles.*', 128)
            if name != name.strip() or name in seen or type(profile) is not NamedPreferenceProfile:
                raise PreferencesError('invalid', 'profiles')
            seen.add(name)
        if (type(self.translator_models) is not tuple or len(self.translator_models) > len(MODEL_TRANSLATORS)
                or any(type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                       or pair[0] not in MODEL_TRANSLATORS
                       for pair in self.translator_models)
                or len({name for name, _ in self.translator_models}) != len(self.translator_models)):
            raise PreferencesError('invalid', 'translator_models')
        for name, value in self.translator_models:
            if not MODEL_TRANSLATORS[name](value):
                raise PreferencesError('invalid', 'translator_models')


def is_valid_model_label(label: str) -> bool:
    """The rule ``translator_models`` enforces, for a caller that asks before saving."""
    try:
        declared_model(label)
    except ProvenanceValidationError:
        return False
    return True


# What each translator's entry names: LibreTranslate a declared label, Ollama an installed model's name.
MODEL_TRANSLATORS = {'libretranslate': is_valid_model_label, 'ollama': valid_observed_model_name}


def preferences_from_mapping(data: object) -> tuple[GlobalPreferences, bool]:
    """Validate known v1 keys; unknown values never enter the returned object."""
    if not isinstance(data, dict):
        raise PreferencesError('invalid')
    unknown = bool(set(data) - set(TOP_KEYS))
    folders = data.get('folders')
    if folders is None:
        folders = {}
    if not isinstance(folders, dict):
        raise PreferencesError('invalid', 'folders')
    unknown |= bool(set(folders) - set(FOLDER_KEYS))
    profiles = data.get('profiles')
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict) or len(profiles) > 64:
        raise PreferencesError('invalid', 'profiles')
    parsed = {}
    for name, profile in profiles.items():
        _text(name, 'profiles.*', 128)
        name = name.strip()
        if name in parsed or not isinstance(profile, dict):
            raise PreferencesError('invalid', 'profiles')
        unknown |= bool(set(profile) - set(PROFILE_KEYS))
        parsed[name] = NamedPreferenceProfile(**{key: profile[key] for key in PROFILE_KEYS if key in profile})
    models = data.get('translator_models', {})
    if not isinstance(models, dict):
        raise PreferencesError('invalid', 'translator_models')
    result = GlobalPreferences(
        version=data.get('version', 1),
        default_target_language=data.get('default_target_language', 'pt'),
        books_dir=data.get('books_dir', '~/Documentos/Livros'),
        folders=PreferenceFolders(**{key: folders[key] for key in FOLDER_KEYS if key in folders}),
        reader_app=data.get('reader_app'), profiles=tuple(sorted(parsed.items())),
        translator_models=tuple(models.items()),
    )
    return result, unknown


def preferences_to_mapping(value: GlobalPreferences) -> dict[str, object]:
    """Explicit authorized persistence payload; never use this for diagnostics."""
    value.__post_init__()
    return {
        **({'translator_models': dict(value.translator_models)} if value.translator_models else {}),
        'version': value.version, 'default_target_language': value.default_target_language,
        'books_dir': value.books_dir,
        'folders': {key: getattr(value.folders, key) for key in FOLDER_KEYS},
        'reader_app': value.reader_app,
        'profiles': {name: {key: getattr(profile, key) for key in PROFILE_KEYS
                            if getattr(profile, key) is not None} for name, profile in value.profiles},
    }


# Configuration values, as the CLI reads them.
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
    translator: str | None = None
    model: str | None = None

    @classmethod
    def from_dict(cls, data: object, profile_name: str) -> "TranslationProfile":
        try:
            value, _ = preferences_from_mapping({'profiles': {'profile': data}})
            profile = value.profiles[0][1]
            return cls(profile.target_language, Path(profile.glossary) if profile.glossary is not None else None,
                       profile.style, profile.translator, profile.model)
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
        if self.translator is not None:
            data['translator'] = self.translator
        if self.model is not None:
            data['model'] = self.model
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
    translator_models: dict[str, str] = field(default_factory=dict, repr=False)

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
            **({"translator_models": dict(self.translator_models)} if self.translator_models else {}),
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




def config_to_preferences(config: AyvuConfig) -> GlobalPreferences:
    return GlobalPreferences(
        version=config.version, default_target_language=config.default_target_language,
        books_dir=str(config.books_dir),
        folders=PreferenceFolders(**config.folders.to_dict()), reader_app=config.reader_app,
        profiles=tuple((name, NamedPreferenceProfile(
            profile.target_language, str(profile.glossary) if profile.glossary is not None else None,
            profile.style, profile.translator, profile.model)) for name, profile in sorted(config.profiles.items())),
        translator_models=tuple(config.translator_models.items()),
    )


def config_from_preferences(value: GlobalPreferences, revision: str | None = None) -> AyvuConfig:
    return AyvuConfig(
        version=value.version, default_target_language=value.default_target_language,
        books_dir=Path(value.books_dir), folders=FolderNames(**{
            key: getattr(value.folders, key) for key in FolderNames.__dataclass_fields__}),
        reader_app=value.reader_app,
        translator_models=dict(value.translator_models),
        profiles={name: TranslationProfile(profile.target_language,
                  Path(profile.glossary) if profile.glossary is not None else None, profile.style, profile.translator, profile.model)
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
