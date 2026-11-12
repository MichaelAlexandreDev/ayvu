"""Bounded review input, destination identities and local failure codes."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from . import ReviewApplicationError


_MAX_PACKAGE_BYTES = 64 * 1024 * 1024


_MAX_V1_BYTES = 32 * 1024 * 1024


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_code(error: Exception, fallback: str) -> str:
    code = str(error)
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else fallback


def read_review_input(path: Path | str) -> bytes:
    """Read one bounded regular package file without following links or echoing its path."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(Path(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReviewApplicationError("unsafe_review_file")
            if info.st_size > _MAX_PACKAGE_BYTES:
                raise ReviewApplicationError("review_file_too_large")
            raw = stream.read(_MAX_PACKAGE_BYTES + 1)
    except ReviewApplicationError:
        raise
    except FileNotFoundError:
        raise ReviewApplicationError("review_file_not_found") from None
    except OSError:
        raise ReviewApplicationError("review_file_unreadable") from None
    if len(raw) > _MAX_PACKAGE_BYTES:
        raise ReviewApplicationError("review_file_too_large")
    if len(raw) > _MAX_V1_BYTES and not raw.startswith(b"AYVU-REVIEW-PACKAGE"):
        raise ReviewApplicationError("review_file_too_large")
    return raw


def destination_fingerprint(path: Path | str) -> str:
    """Stable output target identity that can travel in commands without exposing the path."""
    canonical = str(Path(path).expanduser().resolve(strict=False))
    return _fingerprint(canonical.encode("utf-8"))
