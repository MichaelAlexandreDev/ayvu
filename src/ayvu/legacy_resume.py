"""Read-only view of a legacy ``*.ayvu-state.json`` checkpoint.

It answers what a checkpoint is without importing it into jobs. The view omits
paths and the translator URL, which can identify a book or carry credentials,
and the chapter names, which come from the book.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .resume import RESUME_STATE_VERSION, ResumeStateError, TranslationResumeState

MAX_LEGACY_STATE_BYTES = 8 * 1024 * 1024
_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,34}")


class LegacyResumeError(ValueError):
    """A bounded error: messages never include paths or file content."""


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


def inspect_legacy_resume(path: Path) -> LegacyResumeInspection:
    """Never writes, renames or deletes; the checkpoint keeps working as it does today."""
    try:
        data = json.loads(_read(path).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        if isinstance(error, LegacyResumeError):
            raise
        raise LegacyResumeError("legacy_state_invalid") from None
    if not isinstance(data, dict):
        raise LegacyResumeError("legacy_state_invalid")
    version = data.get("version")
    if type(version) is int and version != RESUME_STATE_VERSION:
        raise LegacyResumeError("legacy_state_unsupported_version")
    try:
        state = TranslationResumeState.from_dict(data)
    except (ResumeStateError, ValueError, TypeError):
        raise LegacyResumeError("legacy_state_invalid") from None
    if not _LANGUAGE.fullmatch(state.source) or not _LANGUAGE.fullmatch(state.target):
        raise LegacyResumeError("legacy_state_invalid")
    return LegacyResumeInspection(
        version=state.version, status=state.status, source=state.source, target=state.target,
        total_chapters=state.total_chapters, completed_chapters=len(state.completed_chapters),
        failed_chapters=len(state.failed_chapters), failed_segments=state.failed_segment_count)
