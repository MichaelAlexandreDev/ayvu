"""Local files as format sources, and the check that a destination is not the source.

Identity comes from the file system, not from comparing path text, so a symlink, a
hard link and ``a/../b`` are all recognized. A check cannot outlive itself: a file
replaced after it ran is caught by the fingerprint, not here.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import BinaryIO

from .format_adapter import AliasVerdict, FormatError
from .format_service import FormatSource

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
_CHUNK_BYTES = 1024 * 1024
_MAX_EXTENSION_CHARS = 17


class LocalFormatSource:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def __repr__(self) -> str:
        return "LocalFormatSource()"

    @property
    def extension(self) -> str | None:
        suffix = self._path.suffix.lower()
        if not 2 <= len(suffix) <= _MAX_EXTENSION_CHARS or not suffix[1:].isascii() or not suffix[1:].isalnum():
            return None
        return suffix

    def open(self) -> BinaryIO:
        # Non-blocking, and checked on the descriptor: opening a FIFO must not hang, and
        # what was opened is what gets checked, whatever the path points at by then.
        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise FormatError("source_unreadable") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise FormatError("source_not_regular_file")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def size(self) -> int:
        with self.open() as stream:
            return os.fstat(stream.fileno()).st_size

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        with self.open() as stream:
            while chunk := stream.read(_CHUNK_BYTES):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def head(self, limit: int) -> bytes:
        with self.open() as stream:
            return stream.read(limit)

    def identity(self) -> tuple[int, int]:
        with self.open() as stream:
            status = os.fstat(stream.fileno())
        return status.st_dev, status.st_ino


class LocalOutputGuard:
    def __init__(self, destination: Path | str) -> None:
        self._destination = Path(destination)

    def __repr__(self) -> str:
        return "LocalOutputGuard()"

    def verdict(self, source: FormatSource) -> AliasVerdict:
        if type(source) is not LocalFormatSource:
            return AliasVerdict.UNKNOWN
        try:
            identity = source.identity()
            status = os.stat(self._destination)
        except FileNotFoundError:
            # A dangling link leads somewhere that cannot be judged; a missing name cannot be the source.
            return AliasVerdict.UNKNOWN if self._destination.is_symlink() else AliasVerdict.DISTINCT
        except (OSError, FormatError):
            return AliasVerdict.UNKNOWN
        return AliasVerdict.SAME if (status.st_dev, status.st_ino) == identity else AliasVerdict.DISTINCT
