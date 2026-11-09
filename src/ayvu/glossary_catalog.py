"""Framework-neutral managed glossary catalog contracts.

The repository is supplied by a trusted composition root; references alone grant
no filesystem authority. All values that could correlate private data hide repr.
Limits are measured in UTF-8 bytes and Unicode code points, respectively.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol
import re


@dataclass(frozen=True)
class GlossaryLimits:
    file_bytes: int = 1_048_576
    entries: int = 10_000
    term_chars: int = 1_024
    translation_chars: int = 4_096
    serialized_bytes: int = 1_048_576
    catalog_entries: int = 1_000


LIMITS = GlossaryLimits()


@dataclass(frozen=True, repr=False)
class GlossaryReference:
    value: str

    def __post_init__(self) -> None:
        try:
            size = len(self.value.encode('utf-8')) if isinstance(self.value, str) else 256
        except UnicodeError:
            size = 256
        if (not isinstance(self.value, str) or not self.value.endswith('.json')
                or self.value in ('.json', '..json') or '/' in self.value
                or '\\' in self.value or '\x00' in self.value
                or self.value.startswith('.') or size > 255):
            raise ValueError('invalid-reference')


@dataclass(frozen=True, repr=False)
class Entry:
    term: str
    rule: str
    translation: str | None = None
    required: bool = False


@dataclass(frozen=True, repr=False)
class Snapshot:
    reference: GlossaryReference
    entries: tuple[Entry, ...]
    revision: str


@dataclass(frozen=True, repr=False)
class CatalogItem:
    reference: GlossaryReference
    status: str
    count: int | None = None


@dataclass(frozen=True, repr=False)
class Result:
    value: object = None
    failure: str | None = None
    outcome: str = 'success'


class GlossaryRepository(Protocol):
    def list(self, cancel: Callable[[], bool] | None = None) -> Result: ...
    def read(self, reference: GlossaryReference, cancel: Callable[[], bool] | None = None) -> Result: ...
    def create(self, reference: GlossaryReference, entries: tuple[Entry, ...], cancel: Callable[[], bool] | None = None) -> Result: ...
    def update(self, reference: GlossaryReference, entries: tuple[Entry, ...], expected_revision: str, cancel: Callable[[], bool] | None = None) -> Result: ...


def _invoke(method: Callable[..., Result], *args: object) -> Result:
    try:
        result = method(*args)
        if not isinstance(result, Result):
            return Result(failure='internal', outcome='failed')
        if result.failure is not None:
            allowed = {'invalid', 'invalid-reference', 'invalid-revision', 'oversized',
                       'catalog-too-large', 'unsafe-link', 'unsafe-multilink', 'unsafe-root',
                       'stale-revision', 'missing', 'already-exists', 'cancelled',
                       'unsupported', 'io-error', 'internal'}
            return Result(failure=result.failure if result.failure in allowed else 'internal', outcome='failed')
        return result
    except Exception:
        return Result(failure='internal', outcome='failed')


def list_glossaries(repository: GlossaryRepository, cancel: Callable[[], bool] | None = None) -> Result:
    return _invoke(repository.list, cancel)


def read_glossary(repository: GlossaryRepository, reference: GlossaryReference, cancel: Callable[[], bool] | None = None) -> Result:
    if not isinstance(reference, GlossaryReference):
        return Result(failure='invalid-reference', outcome='failed')
    return _invoke(repository.read, reference, cancel)


def create_glossary(repository: GlossaryRepository, reference: GlossaryReference, entries: tuple[Entry, ...], cancel: Callable[[], bool] | None = None) -> Result:
    if not isinstance(reference, GlossaryReference):
        return Result(failure='invalid-reference', outcome='failed')
    if not _valid_entries(entries):
        return Result(failure='invalid', outcome='failed')
    return _invoke(repository.create, reference, entries, cancel)


def update_glossary(repository: GlossaryRepository, reference: GlossaryReference, entries: tuple[Entry, ...], expected_revision: str, cancel: Callable[[], bool] | None = None) -> Result:
    if not isinstance(reference, GlossaryReference):
        return Result(failure='invalid-reference', outcome='failed')
    if not isinstance(expected_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_revision):
        return Result(failure='invalid-revision', outcome='failed')
    if not _valid_entries(entries):
        return Result(failure='invalid', outcome='failed')
    return _invoke(repository.update, reference, entries, expected_revision, cancel)


def _valid_entries(entries: object) -> bool:
    if not isinstance(entries, tuple) or len(entries) > LIMITS.entries:
        return False
    seen: set[str] = set()
    for e in entries:
        if (type(e) is not Entry or not isinstance(e.term, str) or not e.term
                or e.term.strip() != e.term or len(e.term) > LIMITS.term_chars
                or e.term in seen or type(e.required) is not bool
                or e.rule not in ('translate', 'preserve', 'forbidden')
                or (e.rule == 'translate' and (not isinstance(e.translation, str)
                    or len(e.translation) > LIMITS.translation_chars))
                or (e.rule != 'translate' and e.translation is not None)
                or (e.rule == 'forbidden' and e.required)):
            return False
        seen.add(e.term)
    return True
