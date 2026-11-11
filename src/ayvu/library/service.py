"""Framework-neutral, read-only library discovery and authorized opening."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol
from uuid import uuid4

MAX_ENTRIES = 10000
MAX_WARNINGS = 100
MAX_NAME = 255  # The longest name a common filesystem allows, so no valid file is refused for its length.


@dataclass(frozen=True)
class Warning:
    ref: str
    reason: str


@dataclass(frozen=True)
class Candidate:
    """Adapter-provided metadata; token is private to the filesystem port."""
    name: str
    kind: str
    token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class Entry:
    ref: str
    language: str = ""
    filename: str = ""

    @property
    def label(self) -> str:
        return f"Traduzido - {language_display_name(self.language)}"


@dataclass(frozen=True)
class Book:
    ref: str
    name: str
    original: Entry | None
    translations: tuple[Entry, ...]

    @property
    def has_original(self) -> bool:
        return self.original is not None


@dataclass(frozen=True)
class ScanResult:
    books: tuple[Book, ...]
    warnings: tuple[Warning, ...]
    cancelled: bool = False


@dataclass(frozen=True)
class OpenResult:
    status: str
    display_path: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ScanRequest:
    root_ref: str = field(repr=False)
    cancel: Callable[[], bool] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ListRequest:
    root_ref: str = field(repr=False)


@dataclass(frozen=True)
class ListResult:
    status: str
    books: tuple[Book, ...] = ()


@dataclass(frozen=True)
class DescribeRequest:
    book_ref: str = field(repr=False)


@dataclass(frozen=True)
class DescribeResult:
    status: str
    book: Book | None = None


@dataclass(frozen=True)
class OpenRequest:
    entry_ref: str = field(repr=False)
    cancel: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    include_display_path: bool = False


class Discovery(Protocol):
    root_ref: str
    def discover(self, cancelled: Callable[[], bool], limit: int) -> tuple[list[Candidate], list[Warning], bool]: ...


class Authorization(Protocol):
    def authorize(self, token: object) -> str | None: ...


class Launcher(Protocol):
    def launch(self, path: str, cancelled: Callable[[], bool], revalidate: Callable[[], bool]) -> str: ...


_NAMES = {"de": "Alemão", "en": "Inglês", "es": "Espanhol", "fr": "Francês", "it": "Italiano", "ja": "Japonês", "pt": "Português", "pt-br": "Português (Brasil)"}


def language_display_name(language: str) -> str:
    normalized = language.strip()
    return _NAMES.get(normalized.lower(), normalized) if normalized else "idioma desconhecido"


def _split(stem: str, originals: list[str]) -> tuple[str, str]:
    for name in originals:
        prefix = name + "-"
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return name, stem[len(prefix):]
    parts = stem.split("-")
    if len(parts) >= 3 and parts[-2].isalpha() and len(parts[-2]) in (2, 3) and parts[-1].isalpha() and len(parts[-1]) == 2:
        return "-".join(parts[:-2]) or stem, "-".join(parts[-2:])
    if len(parts) >= 2:
        return "-".join(parts[:-1]) or stem, parts[-1]
    return stem, ""


class LibraryService:
    def __init__(self, filesystem: Discovery, launcher: Launcher, authorization: Authorization):
        self._filesystem = filesystem
        self._authorization = authorization
        self.root_ref = filesystem.root_ref
        self._launcher = launcher
        self._entries: dict[str, object] = {}
        self._books: dict[str, Book] = {}

    def scan_request(self, request: ScanRequest) -> ScanResult:
        if request.root_ref != self.root_ref:
            return ScanResult((), (Warning(self.root_ref, "unknown_root"),))
        return self.scan(request.cancel)

    def list_entries(self, request: ListRequest) -> ListResult:
        if request.root_ref != self.root_ref:
            return ListResult("unknown_root")
        return ListResult("ok", tuple(self._books.values()))

    def describe_entry(self, request: DescribeRequest) -> DescribeResult:
        book = self.describe(request.book_ref)
        return DescribeResult("ok", book) if book is not None else DescribeResult("stale")

    def open_entry(self, request: OpenRequest) -> OpenResult:
        return self.open(request.entry_ref, request.cancel, include_display_path=request.include_display_path)

    def scan(self, cancel: Callable[[], bool] | None = None) -> ScanResult:
        cancelled = cancel or (lambda: False)
        self._entries.clear()
        self._books.clear()
        candidates, warnings, stopped = self._filesystem.discover(cancelled, MAX_ENTRIES)
        warnings = list(warnings[:MAX_WARNINGS])
        valid = []
        for candidate in candidates[:MAX_ENTRIES]:
            if cancelled():
                stopped = True
                break
            name = candidate.name
            if not name or "/" in name or len(name) > MAX_NAME or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name):
                if len(warnings) < MAX_WARNINGS:
                    warnings.append(Warning(uuid4().hex, "invalid_filename"))
                continue
            valid.append(candidate)
        originals = {c.name: c for c in valid if c.kind == "original"}
        original_names = sorted(originals, key=lambda value: (-len(value), value))
        grouped: dict[str, list[tuple[str, Candidate]]] = {}
        for c in valid:
            if cancelled():
                stopped = True
                break
            if c.kind == "translation":
                name, language = _split(c.name, original_names)
                grouped.setdefault(name, []).append((language, c))
        def entry(c: Candidate, language: str = "") -> Entry:
            ref = uuid4().hex
            self._entries[ref] = c.token
            return Entry(ref, language, c.name + ".epub")
        books = []
        for name in sorted(set(originals) | set(grouped), key=lambda n: (n.casefold(), n)):
            if cancelled():
                stopped = True
                break
            translations = sorted(grouped.get(name, []), key=lambda pair: (language_display_name(pair[0]).casefold(), pair[1].name.casefold(), pair[1].name))
            book = Book(uuid4().hex, name, entry(originals[name]) if name in originals else None, tuple(entry(c, lang) for lang, c in translations))
            books.append(book)
            self._books[book.ref] = book
        return ScanResult(tuple(books), tuple(warnings), stopped)

    def describe(self, ref: str) -> Book | None:
        return self._books.get(ref)

    def open(self, ref: str, cancel: Callable[[], bool] | None = None, *, include_display_path: bool = False) -> OpenResult:
        cancelled = cancel or (lambda: False)
        if cancelled():
            return OpenResult("cancelled")
        token = self._entries.get(ref)
        if token is None:
            return OpenResult("stale")
        path = self._authorization.authorize(token)
        if path is None:
            return OpenResult("unauthorized")
        if cancelled():
            return OpenResult("cancelled")
        status = self._launcher.launch(path, cancelled, lambda: self._authorization.authorize(token) == path)
        return OpenResult(status, path if status == "opened" and include_display_path else None)
