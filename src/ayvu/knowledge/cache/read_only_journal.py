"""Classify a rollback journal without opening SQLite or changing any locks.

A short isolated process queries SQLite's RESERVED byte with F_GETLK. Opening
and closing a descriptor in the caller would release its own POSIX locks, so
the query must run outside the caller. No database content is read anywhere.
"""
from __future__ import annotations

import subprocess
import sys

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
