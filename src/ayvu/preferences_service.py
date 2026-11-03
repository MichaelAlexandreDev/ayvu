"""Shared get/update preferences use cases and repository port."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from .preferences import GlobalPreferences, PreferencesError


@dataclass(frozen=True)
class GetPreferences:
    pass


@dataclass(frozen=True, repr=False)
class UpdatePreferences:
    preferences: GlobalPreferences
    expected_revision: str

    def __post_init__(self) -> None:
        if type(self.preferences) is not GlobalPreferences:
            raise PreferencesError('invalid')
        validate_revision(self.expected_revision)


@dataclass(frozen=True, repr=False)
class PreferencesSnapshot:
    preferences: GlobalPreferences | None
    revision: str | None
    exists: bool
    read_only: bool = False
    warnings: tuple[str, ...] = ()
    error: PreferencesError | None = None


@dataclass(frozen=True, repr=False)
class UpdatePreferencesResult:
    effective: GlobalPreferences
    persisted: bool
    snapshot: PreferencesSnapshot | None = None
    error: PreferencesError | None = None


class PreferencesRepository(Protocol):
    def load(self) -> PreferencesSnapshot: ...
    def save(self, preferences: GlobalPreferences, expected_revision: str) -> PreferencesSnapshot: ...


def validate_revision(value: str) -> None:
    if not isinstance(value, str) or (value != 'missing' and not re.fullmatch(r'[0-9a-f]{64}', value)):
        raise PreferencesError('invalid_revision', 'revision')


def get_preferences(repository: PreferencesRepository, request: GetPreferences | None = None) -> PreferencesSnapshot:
    try:
        return repository.load()
    except PreferencesError as exc:
        return PreferencesSnapshot(None, None, False, error=PreferencesError(exc.code, exc.field))


def update_preferences(repository: PreferencesRepository, request: UpdatePreferences) -> UpdatePreferencesResult:
    try:
        if type(request.preferences) is not GlobalPreferences:
            raise PreferencesError('invalid')
        request.preferences.__post_init__()
        validate_revision(request.expected_revision)
        snapshot = repository.save(request.preferences, request.expected_revision)
        return UpdatePreferencesResult(request.preferences, True, snapshot)
    except PreferencesError as exc:
        return UpdatePreferencesResult(request.preferences, False, error=PreferencesError(exc.code, exc.field))
