"""Bounded v1 JSON persistence with serialized expected-revision updates."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .preferences import (
    GlobalPreferences, PreferencesError, preferences_from_mapping, preferences_to_mapping,
)
from .preferences_service import PreferencesSnapshot, validate_revision

MAX_CONFIG_BYTES = 128 * 1024
LOCK_TIMEOUT_SECONDS = 0.5


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreferencesError('malformed')
        result[key] = value
    return result


def _reject_constant(value):
    raise PreferencesError('malformed')


def _safe_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    if any(item.is_symlink() for item in (*absolute.parents, absolute)):
        raise PreferencesError('unsafe_path')
    return absolute


def _private_parent(path: Path) -> None:
    """Reject ancestors another OS user could replace; do not chmod user folders."""
    if os.name == 'nt':
        return  # Native ACL guarantees require separate Windows validation.
    for directory in (path.parent, *path.parent.parents):
        try:
            info = directory.stat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise PreferencesError('unsafe_path')


@contextmanager
def _storage_errors():
    try:
        yield
    except PreferencesError:
        raise
    except PermissionError:
        raise PreferencesError('permission') from None
    except OSError:
        raise PreferencesError('io') from None
    except (ValueError, UnicodeError, RecursionError, RuntimeError):
        raise PreferencesError('malformed') from None


@contextmanager
def _exclusive_lock(path: Path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(_safe_path(path), flags, 0o600), 'r+b') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
            raise PreferencesError('unsafe_path')
        if info.st_size == 0:
            stream.write(b'0')
            stream.flush()
        locked = False
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, PermissionError):
                    if time.monotonic() >= deadline:
                        raise PreferencesError('busy') from None
                    time.sleep(0.01)
            yield
        finally:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class JsonPreferencesRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> PreferencesSnapshot:
        path = _safe_path(self.path)
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            fd = os.open(path, flags)
        except FileNotFoundError:
            return PreferencesSnapshot(GlobalPreferences(), 'missing', False)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PreferencesError('unsafe_path')
            if info.st_size > MAX_CONFIG_BYTES:
                raise PreferencesError('oversized')
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                payload = stream.read(MAX_CONFIG_BYTES + 1)
            if len(payload) > MAX_CONFIG_BYTES:
                raise PreferencesError('oversized')
        finally:
            if fd is not None:
                os.close(fd)
        try:
            data = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise PreferencesError('malformed') from None
        preferences, unknown = preferences_from_mapping(data)
        return PreferencesSnapshot(preferences, hashlib.sha256(payload).hexdigest(), True,
                                   read_only=unknown, warnings=('unknown_fields',) if unknown else ())

    def load(self) -> PreferencesSnapshot:
        # No mkdir, lock file or canonical rewrite as a side effect of GET.
        with _storage_errors():
            return self._read()

    def save(self, preferences: GlobalPreferences, expected_revision: str) -> PreferencesSnapshot:
        with _storage_errors():
            validate_revision(expected_revision)
            if type(preferences) is not GlobalPreferences:
                raise PreferencesError('invalid')
            payload = (json.dumps(preferences_to_mapping(preferences), ensure_ascii=False,
                                  sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
            if len(payload) > MAX_CONFIG_BYTES:
                raise PreferencesError('oversized')
            path = _safe_path(self.path)
            _private_parent(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _private_parent(path)
            with _exclusive_lock(path.with_name('.' + path.name + '.lock')):
                current = self._read()
                if current.read_only:
                    raise PreferencesError('unknown_fields')
                if current.revision != expected_revision:
                    raise PreferencesError('conflict')
                self._publish(path, payload)
            return PreferencesSnapshot(preferences, hashlib.sha256(payload).hexdigest(), True)

    @staticmethod
    def _publish(path: Path, payload: bytes) -> None:
        # mkstemp supplies private permissions. Keep an existing stricter mode.
        mode = (path.stat().st_mode & 0o600) if path.exists() else 0o600
        fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                if os.name != 'nt':
                    os.fchmod(stream.fileno(), mode)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != 'nt':
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    try:
                        os.fsync(directory_fd)
                    except OSError:
                        raise PreferencesError('commit_uncertain') from None
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
