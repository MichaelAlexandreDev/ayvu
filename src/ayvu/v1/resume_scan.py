"""Read-only view of a legacy ``*.ayvu-state.json`` checkpoint.

It answers what a checkpoint is without importing it into jobs. The view omits
paths and the translator URL, which can identify a book or carry credentials,
and the chapter names, which come from the book.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .resume import RESUME_STATE_VERSION, STATE_FILE_PATTERN, ResumeStateError, TranslationResumeState

MAX_LEGACY_STATE_BYTES = 8 * 1024 * 1024
MAX_LEGACY_STATES = 1024
_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,34}")


class LegacyResumeError(ValueError):
    """A bounded error: messages never include paths or file content.

    ``detail`` is what the parser said about the file, kept apart for the presentation that chose to show it.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.detail = detail


@dataclass(frozen=True)
class LegacyResumeInspection:
    version: int
    status: str
    source: str
    target: str
    total_chapters: int | None
    completed_chapters: int
    failed_chapters: int
    failed_segments: int


def _read(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise LegacyResumeError("legacy_state_not_found") from None
    except OSError:
        raise LegacyResumeError("legacy_state_unreadable") from None
    # Checked on the descriptor: wrapping a directory in a stream would raise first.
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise LegacyResumeError("legacy_state_unreadable")
    with os.fdopen(fd, "rb") as stream:
        payload = stream.read(MAX_LEGACY_STATE_BYTES + 1)
    if len(payload) > MAX_LEGACY_STATE_BYTES:
        raise LegacyResumeError("legacy_state_too_large")
    return payload


def _load(path: Path) -> TranslationResumeState:
    try:
        data = json.loads(_read(path).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        if isinstance(error, LegacyResumeError):
            raise
        raise LegacyResumeError("legacy_state_invalid") from None
    version = data.get("version") if isinstance(data, dict) else None
    code = "legacy_state_unsupported_version" if type(version) is int and version != RESUME_STATE_VERSION else "legacy_state_invalid"
    try:
        state = TranslationResumeState.from_dict(data)
    except (ResumeStateError, ValueError, TypeError) as error:
        raise LegacyResumeError(code, str(error)) from None
    if not _LANGUAGE.fullmatch(state.source) or not _LANGUAGE.fullmatch(state.target):
        raise LegacyResumeError("legacy_state_invalid", "Resume state languages are not valid.")
    return state


@dataclass(frozen=True, repr=False)
class LegacyStateEntry:
    """One ``*.ayvu-state.json``. ``state`` is None if it could not be read, and then ``failure`` says why.

    ``path``, ``state`` and ``message`` are for a presentation that chose to show them, so the repr omits them.
    """

    reference: str
    path: Path
    state: TranslationResumeState | None = None
    failure: str | None = None
    message: str | None = None

    def __repr__(self) -> str:
        return f"LegacyStateEntry(reference={self.reference!r}, failure={self.failure!r})"


def _reference(path: Path) -> str:
    return "legacy:" + hashlib.sha256(path.name.encode()).hexdigest()


def _entry(path: Path) -> LegacyStateEntry:
    try:
        return LegacyStateEntry(_reference(path), path, _load(path))
    except LegacyResumeError as error:
        code = str(error)
        reason = "is not valid JSON" if error.detail is None and code == "legacy_state_invalid" else None
        message = (f"Resume state file {reason}: {path}" if reason else
                   f"Invalid resume state file {path}: {error.detail or code}")
        return LegacyStateEntry(_reference(path), path, failure=code, message=message)


def scan_legacy_states(processing_dir: Path) -> tuple[LegacyStateEntry, ...]:
    """Every state file of a folder, by name, each one bounded. It changes nothing, and a bad file is an entry."""
    try:
        paths = sorted(processing_dir.glob(STATE_FILE_PATTERN))
    except OSError:
        return ()
    return tuple(_entry(path) for path in paths[:MAX_LEGACY_STATES])


def inspect_legacy_resume(path: Path) -> LegacyResumeInspection:
    """Never writes, renames or deletes; the checkpoint keeps working as it does today."""
    state = _load(path)
    return LegacyResumeInspection(
        version=state.version, status=state.status, source=state.source, target=state.target,
        total_chapters=state.total_chapters, completed_chapters=len(state.completed_chapters),
        failed_chapters=len(state.failed_chapters), failed_segments=state.failed_segment_count)
