"""POSIX managed-directory JSON adapter for the glossary catalog."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import unicodedata

try:
    import fcntl
except ImportError:  # No safe cross-process lock implementation on this platform.
    fcntl = None

from .glossary import Glossary, GlossaryEntry, glossary_to_dict, _parse_glossary_entries
from .glossary_catalog import CatalogItem, Entry, GlossaryReference, LIMITS, Result, Snapshot


class _Failure(Exception):
    pass


def _failed(code: str) -> Result:
    return Result(failure=code, outcome='failed')


def _cancel(cancel) -> None:
    if cancel is not None and cancel():
        raise _Failure('cancelled')


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _key(name: str) -> str:
    return unicodedata.normalize('NFC', name).casefold()


def _revision(scope: bytes, name: str, data: bytes, st) -> str:
    h = hashlib.sha256()
    h.update(scope)
    h.update(b'\0')
    h.update(name.encode('utf-8'))
    h.update(b'\0')
    h.update(repr(_identity(st)).encode('ascii'))
    h.update(b'\0')
    h.update(data)
    return h.hexdigest()


def _entries(data: bytes) -> tuple[Entry, ...]:
    if len(data) > LIMITS.file_bytes:
        raise _Failure('oversized')
    try:
        obj = json.loads(data.decode('utf-8'))
        if not isinstance(obj, dict) or len(obj) > LIMITS.entries:
            raise ValueError()
        parsed = _parse_glossary_entries(obj)
        result = tuple(Entry(e.term, e.rule, e.translation, e.required) for e in parsed)
        _validate(result)
        return result
    except _Failure:
        raise
    except Exception:
        raise _Failure('invalid') from None


def _validate(entries) -> None:
    if not isinstance(entries, tuple) or len(entries) > LIMITS.entries:
        raise _Failure('invalid')
    seen = set()
    for e in entries:
        if (not isinstance(e, Entry) or not isinstance(e.term, str) or not e.term
                or e.term.strip() != e.term or len(e.term) > LIMITS.term_chars
                or not isinstance(e.required, bool) or e.term in seen
                or e.rule not in ('translate', 'preserve', 'forbidden')
                or (e.rule == 'translate' and (not isinstance(e.translation, str) or len(e.translation) > LIMITS.translation_chars))
                or (e.rule != 'translate' and e.translation is not None)
                or (e.rule == 'forbidden' and e.required)):
            raise _Failure('invalid')
        seen.add(e.term)


def _serialize(entries) -> bytes:
    _validate(entries)
    try:
        glossary = Glossary(GlossaryEntry(e.term, e.rule, e.translation, e.required) for e in entries)
        data = (json.dumps(glossary_to_dict(glossary), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if len(data) > LIMITS.serialized_bytes:
            raise _Failure('oversized')
        _entries(data)
        return data
    except _Failure:
        raise
    except Exception:
        raise _Failure('invalid') from None


class ManagedGlossaryRepository:
    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()
        self._scope = os.fsencode(self._root)

    @contextlib.contextmanager
    def _directory(self, create=False):
        if fcntl is None or not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY')):
            raise _Failure('unsupported')
        if create:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise _Failure('missing') from None
        try:
            st = os.fstat(fd)
            now = os.stat(self._root, follow_symlinks=False)
            if not stat.S_ISDIR(now.st_mode) or (st.st_dev, st.st_ino) != (now.st_dev, now.st_ino):
                raise _Failure('unsafe-root')
            yield fd
            now = os.stat(self._root, follow_symlinks=False)
            if (st.st_dev, st.st_ino) != (now.st_dev, now.st_ino):
                raise _Failure('unsafe-root')
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _lock(self, fd):
        lock = os.open('.ayvu-glossary.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            st = os.fstat(lock)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise _Failure('unsafe-link')
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._check_lock(fd, lock)
            yield lock
        finally:
            os.close(lock)

    def _check_lock(self, fd, lock):
        opened = os.fstat(lock)
        current = os.stat('.ayvu-glossary.lock', dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or opened.st_nlink != 1
                or _identity(opened) != _identity(current)):
            raise _Failure('unsafe-link')

    def _check_root(self, fd):
        opened = os.fstat(fd)
        current = os.stat(self._root, follow_symlinks=False)
        if (not stat.S_ISDIR(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise _Failure('unsafe-root')

    def _names(self, fd):
        names = sorted(n for n in os.listdir(fd) if n.endswith('.json') and not n.startswith('.'))
        if len(names) > LIMITS.catalog_entries:
            raise _Failure('catalog-too-large')
        return names

    def _read(self, fd, name):
        try:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            raise _Failure('missing') from None
        if stat.S_ISLNK(before.st_mode):
            raise _Failure('unsafe-link')
        if not stat.S_ISREG(before.st_mode):
            raise _Failure('invalid')
        if before.st_nlink != 1:
            raise _Failure('unsafe-multilink')
        if before.st_size > LIMITS.file_bytes:
            raise _Failure('oversized')
        try:
            f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            raise _Failure('stale-revision') from None
        except OSError:
            raise _Failure('unsafe-link') from None
        try:
            opened = os.fstat(f)
            if _identity(before) != _identity(opened) or opened.st_nlink != 1:
                raise _Failure('stale-revision')
            chunks = []
            remaining = LIMITS.file_bytes + 1
            while remaining:
                chunk = os.read(f, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            after = os.fstat(f)
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _Failure('stale-revision') from None
            if _identity(opened) != _identity(after) or _identity(after) != _identity(current):
                raise _Failure('stale-revision')
            if len(data) > LIMITS.file_bytes:
                raise _Failure('oversized')
            return data, after
        finally:
            os.close(f)

    def list(self, cancel=None):
        try:
            _cancel(cancel)
            try:
                with self._directory() as fd:
                    names = self._names(fd)
                    result = []
                    for name in names:
                        _cancel(cancel)
                        try:
                            ref = GlossaryReference(name)
                        except ValueError:
                            continue
                        try:
                            data, _ = self._read(fd, name)
                            entries = _entries(data)
                            result.append(CatalogItem(ref, 'valid', len(entries)))
                        except _Failure as exc:
                            result.append(CatalogItem(ref, str(exc) if str(exc) in ('invalid', 'oversized', 'unsafe-link', 'unsafe-multilink') else 'invalid'))
                    return Result(tuple(result))
            except _Failure as exc:
                if str(exc) == 'missing':
                    return Result(())
                raise
        except _Failure as exc:
            return _failed(str(exc))
        except Exception:
            return _failed('io-error')

    def read(self, reference, cancel=None):
        try:
            _cancel(cancel)
            if type(reference) is not GlossaryReference:
                raise _Failure('invalid-reference')
            GlossaryReference(reference.value)
            with self._directory() as fd:
                data, st = self._read(fd, reference.value)
                entries = _entries(data)
                return Result(Snapshot(reference, entries, _revision(self._scope, reference.value, data, st)))
        except _Failure as exc:
            return _failed(str(exc))
        except Exception:
            return _failed('io-error')

    @staticmethod
    def _restore(fd, name, data) -> None:
        """Best effort after a failed sync: the rename happened, and the caller is told it did not succeed."""
        back = '.ayvu-glossary-' + secrets.token_hex(16)
        try:
            out = os.open(back, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(back, name, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(back, dir_fd=fd)

    def create(self, reference, entries, cancel=None):
        return self._write(reference, entries, None, cancel)

    def update(self, reference, entries, expected_revision, cancel=None):
        return self._write(reference, entries, expected_revision, cancel)

    def _write(self, reference, entries, expected, cancel):
        temp = None
        try:
            _cancel(cancel)
            if type(reference) is not GlossaryReference:
                raise _Failure('invalid-reference')
            GlossaryReference(reference.value)
            data = _serialize(entries)
            with self._directory(create=expected is None) as fd:
                with self._lock(fd) as lock:
                    _cancel(cancel)
                    aliases = [n for n in self._names(fd) if _key(n) == _key(reference.value)]
                    if expected is None:
                        if aliases:
                            raise _Failure('already-exists')
                    else:
                        if aliases != [reference.value]:
                            raise _Failure('stale-revision')
                        try:
                            old, st = self._read(fd, reference.value)
                        except _Failure as exc:
                            if str(exc) == 'missing':
                                raise _Failure('stale-revision') from None
                            raise
                        if _revision(self._scope, reference.value, old, st) != expected:
                            raise _Failure('stale-revision')
                        _entries(old)
                        if old == data:
                            return Result(Snapshot(reference, entries, expected), outcome='already-current')
                    temp = '.ayvu-glossary-' + secrets.token_hex(16)
                    out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    try:
                        with os.fdopen(out, 'wb') as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        _cancel(cancel)
                        self._check_root(fd)
                        self._check_lock(fd, lock)
                        if expected is None:
                            try:
                                os.link(temp, reference.value, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                            except FileExistsError:
                                raise _Failure('already-exists') from None
                            created = os.stat(reference.value, dir_fd=fd, follow_symlinks=False)
                            os.unlink(temp, dir_fd=fd)
                            temp = None
                        else:
                            current, current_st = self._read(fd, reference.value)
                            if _revision(self._scope, reference.value, current, current_st) != expected:
                                raise _Failure('stale-revision')
                            # A directory that cannot sync fails here, before the file is touched.
                            os.fsync(fd)
                            # Same-object revalidation under the lock: a process ignoring the lock could
                            # still swap the file between that check and this rename, which POSIX cannot
                            # make conditional. See docs/architecture/glossary-catalog.md.
                            os.replace(temp, reference.value, src_dir_fd=fd, dst_dir_fd=fd)
                            temp = None
                        try:
                            os.fsync(fd)
                        except OSError:
                            # Best effort only: directory fsync already failed, so
                            # neither rollback nor its durability can be guaranteed.
                            if expected is None:
                                try:
                                    current = os.stat(reference.value, dir_fd=fd, follow_symlinks=False)
                                    if (stat.S_ISREG(current.st_mode) and current.st_nlink == 1
                                            and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)):
                                        os.unlink(reference.value, dir_fd=fd)
                                except OSError:
                                    pass
                            else:
                                self._restore(fd, reference.value, old)
                            raise
                        final, final_st = self._read(fd, reference.value)
                        return Result(Snapshot(reference, _entries(final), _revision(self._scope, reference.value, final, final_st)))
                    finally:
                        if temp is not None:
                            try:
                                os.unlink(temp, dir_fd=fd)
                            except FileNotFoundError:
                                pass
        except _Failure as exc:
            return _failed(str(exc))
        except Exception:
            return _failed('io-error')
