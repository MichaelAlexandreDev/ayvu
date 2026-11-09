"""Immutable version-1 preference values, with no filesystem or JSON dependency."""
from __future__ import annotations

from dataclasses import dataclass
import re

FOLDER_KEYS = ('original', 'translated', 'preview', 'reports', 'processing')
TOP_KEYS = ('version', 'default_target_language', 'books_dir', 'folders', 'reader_app', 'profiles')
PROFILE_KEYS = ('target_language', 'glossary', 'style')
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

    def __post_init__(self) -> None:
        _language(self.target_language, 'profiles.*.target_language', nullable=True)
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
    result = GlobalPreferences(
        version=data.get('version', 1),
        default_target_language=data.get('default_target_language', 'pt'),
        books_dir=data.get('books_dir', '~/Documentos/Livros'),
        folders=PreferenceFolders(**{key: folders[key] for key in FOLDER_KEYS if key in folders}),
        reader_app=data.get('reader_app'), profiles=tuple(sorted(parsed.items())),
    )
    return result, unknown


def preferences_to_mapping(value: GlobalPreferences) -> dict[str, object]:
    """Explicit authorized persistence payload; never use this for diagnostics."""
    value.__post_init__()
    return {
        'version': value.version, 'default_target_language': value.default_target_language,
        'books_dir': value.books_dir,
        'folders': {key: getattr(value.folders, key) for key in FOLDER_KEYS},
        'reader_app': value.reader_app,
        'profiles': {name: {key: getattr(profile, key) for key in PROFILE_KEYS
                            if getattr(profile, key) is not None} for name, profile in value.profiles},
    }
