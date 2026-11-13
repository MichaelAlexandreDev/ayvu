"""Classify a rollback journal without opening SQLite or changing any locks.

A short isolated process queries SQLite's RESERVED byte with F_GETLK. Opening
and closing a descriptor in the caller would release its own POSIX locks, so
the query must run outside the caller. No database content is read anywhere.
"""
from __future__ import annotations

import os
import subprocess
import sys
from threading import Lock

_PROBE = """
import fcntl, os, struct, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
try:
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (int(sys.argv[2]), int(sys.argv[3])):
        print('cache_identity_changed')
    elif struct.calcsize('l') != 8:
        print('cache_busy')
    else:
        query = struct.pack('hhqqi4x', fcntl.F_WRLCK, os.SEEK_SET, 0x40000001, 1, 0)
        answer = fcntl.fcntl(fd, fcntl.F_GETLK, query)
        kind = struct.unpack('hhqqi4x', answer)[0]
        print('cache_recovery_required' if kind == fcntl.F_UNLCK else 'cache_busy')
finally:
    os.close(fd)
"""


def journal_refusal(path, expected):
    if sys.platform != "linux":
        return "cache_busy"
    try:
        probe = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _PROBE, str(path),
                                str(expected[0]), str(expected[1])],
                               capture_output=True, text=True, timeout=1, check=True)
        code = probe.stdout.strip()
        return code if code in {"cache_busy", "cache_identity_changed", "cache_recovery_required"} else "cache_busy"
    except (OSError, subprocess.SubprocessError):
        # No reliable absence-of-writer evidence: never diagnose recovery.
        return "cache_busy"


_HEADER = """
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
try:
    info = os.fstat(fd)
    header = os.read(fd, 100)
    if (info.st_dev, info.st_ino) != (int(sys.argv[2]), int(sys.argv[3])):
        print('cache_identity_changed')
    elif len(header) != 100 or header[:16] != b'SQLite format 3\\x00':
        print('cache_corrupt')
    elif header[18:20] != b'\\x01\\x01':
        print('cache_recovery_required')
finally:
    os.close(fd)
"""


_HEALTHY_HEADERS: set[tuple[int, ...]] = set()
_HEALTHY_LOCK = Lock()


def _signature(path):
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def header_refusal(path, expected):
    """Never close a raw database descriptor in the SQLite readers' process.

    POSIX record locks belong to the process: closing any descriptor for the
    database would release another thread's SHARED lock. The child owns no
    parent locks. Inode/marker checks around SQLite use stat exclusively.
    A healthy verdict is remembered per file state (inode, size and both
    timestamps), so the child runs again only after the file changes.
    """
    try:
        signature = _signature(path)
    except OSError:
        return "cache_identity_changed"
    with _HEALTHY_LOCK:
        if signature in _HEALTHY_HEADERS:
            return None
    refusal = _probe_header(path, expected)
    if refusal is None and signature[:2] == tuple(expected):
        with _HEALTHY_LOCK:
            if len(_HEALTHY_HEADERS) >= 64:
                _HEALTHY_HEADERS.clear()
            _HEALTHY_HEADERS.add(signature)
    return refusal


def _probe_header(path, expected):
    try:
        probe = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _HEADER, str(path),
                                str(expected[0]), str(expected[1])],
                               capture_output=True, text=True, timeout=5, check=True)
        code = probe.stdout.strip()
        if not code:
            return None
        return code if code in {"cache_identity_changed", "cache_corrupt", "cache_recovery_required"} else "cache_busy"
    except (OSError, subprocess.SubprocessError):
        return "cache_busy"
