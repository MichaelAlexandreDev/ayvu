"""Atomic publication of sensitive visible text as local Markdown files."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .content_use_cases import (
    OutputDirectoryRef, PlannedOutputFile, PublishedFileRef,
    WriterAuthorization, WriterPlan, WriterPublication,
)
from .format_adapter import AliasVerdict, VisibleContentDocument
from .format_local import LocalFormatSource, LocalOutputGuard
from .format_service import FormatSource
from .job_events import JobFailure, JobFailureFamily

# Not accepted in a file name on some system. A real chapter with one still exports, under a portable name.
_NOT_PORTABLE = re.compile(r'[<>:"\\|?*\x00-\x1f\x7f]')
_MAX_NAME_BYTES = 255
# What a filesystem without hard links answers, such as a FAT or exFAT pen drive.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK})


@dataclass(frozen=True, repr=False)
class _AuthorizedRoot:
    path: Path
    overwrite: bool
    device: int
    inode: int

    def __repr__(self) -> str:
        return "_AuthorizedRoot()"


@dataclass(frozen=True, repr=False)
class _PlannedTarget:
    authorization: object
    path: Path
    ordinal: int
    name: str

    def __repr__(self) -> str:
        return f"_PlannedTarget(ordinal={self.ordinal})"


class MarkdownDirectoryWriter:
    """Preserves the existing Markdown text shape and safely publishes one file at a time."""

    def __repr__(self) -> str:
        return "MarkdownDirectoryWriter()"

    def authorize(self, destination: OutputDirectoryRef, *, overwrite: bool) -> WriterAuthorization:
        try:
            requested = Path(destination.value)
            root = requested.resolve(strict=False)
            if root.exists() and not root.is_dir():
                return WriterAuthorization(failure=_failure(JobFailureFamily.INPUT, "output_not_directory"))
            if root.exists() and not overwrite and any(root.iterdir()):
                return WriterAuthorization(failure=_failure(JobFailureFamily.INPUT, "output_not_empty"))
            if root.exists() and not os.access(root, os.W_OK | os.X_OK):
                return WriterAuthorization(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
            if not root.exists():
                parent = root.parent
                while not parent.exists() and parent != parent.parent:
                    parent = parent.parent
                if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
                    return WriterAuthorization(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
            self._ensure_root_path(root)
            info = root.stat()
            return WriterAuthorization(token=_AuthorizedRoot(root, overwrite, info.st_dev, info.st_ino))
        except PermissionError:
            return WriterAuthorization(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
        except OSError as error:
            return WriterAuthorization(failure=_io_failure(error))
        except (TypeError, ValueError):
            return WriterAuthorization(failure=_failure(JobFailureFamily.POLICY, "invalid_output_directory"))

    def plan(self, authorization: WriterAuthorization, source: FormatSource,
             documents: tuple[VisibleContentDocument, ...]) -> WriterPlan:
        root = self._root(authorization)
        if root is None:
            return WriterPlan(failure=_failure(JobFailureFamily.INTERNAL, "invalid_writer_authorization"))
        if not self._root_is_current(root):
            return WriterPlan(failure=_failure(JobFailureFamily.POLICY, "output_path_changed"))
        targets: list[PlannedOutputFile] = []
        seen: set[str] = set()
        for ordinal, document in enumerate(documents, start=1):
            if type(document) is not VisibleContentDocument:
                return WriterPlan(failure=_failure(JobFailureFamily.INTERNAL, "invalid_visible_content"))
            name, failure = self._name(ordinal, document.output_stem)
            if failure is not None:
                return WriterPlan(failure=failure)
            try:
                candidate = root.path / name
                target = candidate.resolve(strict=False)
                if not target.is_relative_to(root.path):
                    return WriterPlan(failure=_failure(JobFailureFamily.POLICY, "output_path_escape"))
                if candidate.is_symlink():
                    return WriterPlan(failure=_failure(JobFailureFamily.POLICY, "output_symlink"))
                canonical = unicodedata.normalize("NFC", os.path.normcase(str(target))).casefold()
                if canonical in seen:
                    return WriterPlan(failure=_failure(JobFailureFamily.INPUT, "duplicate_output_target"))
                seen.add(canonical)
                if target.exists() and not target.is_file():
                    return WriterPlan(failure=_failure(JobFailureFamily.INPUT, "output_conflict"))
                if target.exists() and not root.overwrite:
                    return WriterPlan(failure=_failure(JobFailureFamily.INPUT, "output_conflict"))
                verdict = LocalOutputGuard(target).verdict(source)
                if verdict is AliasVerdict.SAME:
                    return WriterPlan(failure=_failure(JobFailureFamily.POLICY, "source_alias"))
                if verdict is AliasVerdict.UNKNOWN:
                    return WriterPlan(failure=_failure(JobFailureFamily.POLICY, "source_alias_unknown"))
                token = _PlannedTarget(authorization.token, target, ordinal, name)
                targets.append(PlannedOutputFile(ordinal, token))
            except PermissionError:
                return WriterPlan(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
            except OSError as error:
                return WriterPlan(failure=_io_failure(error))
        return WriterPlan(files=tuple(targets))

    def publish(self, authorization: WriterAuthorization, planned: PlannedOutputFile,
                document: VisibleContentDocument, source: FormatSource, *,
                before_publish: Callable[[], JobFailure | None]) -> WriterPublication:
        root = self._root(authorization)
        target = planned.token if type(planned.token) is _PlannedTarget else None
        if (root is None or target is None or target.authorization is not authorization.token
                or target.ordinal != planned.ordinal or type(document) is not VisibleContentDocument):
            return WriterPublication(failure=_failure(JobFailureFamily.INTERNAL, "invalid_planned_output"))
        if not self._target_is_safe(root, target):
            return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "output_path_changed"))
        root_fd: int | None = None
        staged_name: str | None = None
        try:
            self._ensure_root(root)
            root_fd = self._open_root(root)
            if root_fd is None:
                return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "output_path_changed"))
        except PermissionError:
            return WriterPublication(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
        except OSError as error:
            return WriterPublication(failure=_io_failure(error))

        try:
            payload = self._render(document)
            staged_name = f".ayvu-{secrets.token_hex(16)}.part"
            # The output is an ordinary file of the user, so it follows the umask, like the translated EPUB.
            descriptor = os.open(staged_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                 0o666, dir_fd=root_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not self._valid_stage_at(root_fd, staged_name, payload):
                return WriterPublication(failure=_failure(JobFailureFamily.INTEGRITY, "staged_payload_mismatch"))

            failure = before_publish()
            if failure is not None:
                return WriterPublication(failure=failure)
            if not self._target_is_safe(root, target):
                return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "output_path_changed"))
            verdict = self._alias_at(source, target.name, root_fd)
            if verdict is AliasVerdict.SAME:
                return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "source_alias"))
            if verdict is AliasVerdict.UNKNOWN:
                return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "source_alias_unknown"))
            if not root.overwrite:
                try:
                    self._publish_new(staged_name, target.name, root_fd)
                except FileExistsError:
                    return WriterPublication(failure=_failure(JobFailureFamily.INPUT, "output_conflict"))
            else:
                self._keep_existing_mode(staged_name, target.name, root_fd)
                os.replace(staged_name, target.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            try:
                os.unlink(staged_name, dir_fd=root_fd)
            except OSError:
                pass
            staged_name = None
            return WriterPublication(published_file=PublishedFileRef(target.ordinal, target.name))
        except PermissionError:
            return WriterPublication(failure=_failure(JobFailureFamily.RESOURCE, "output_permission_denied"))
        except FileExistsError:
            return WriterPublication(failure=_failure(JobFailureFamily.INPUT, "output_conflict"))
        except OSError as error:
            return WriterPublication(failure=_io_failure(error))
        except (UnicodeError, ValueError):
            return WriterPublication(failure=_failure(JobFailureFamily.POLICY, "invalid_visible_content"))
        finally:
            if staged_name is not None and root_fd is not None:
                try:
                    os.unlink(staged_name, dir_fd=root_fd)
                except OSError:
                    pass
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _publish_new(staged_name: str, target_name: str, root_fd: int) -> None:
        try:
            os.link(staged_name, target_name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            return
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno not in _NO_HARD_LINKS:
                raise
        # An exclusive create takes the name, so this cannot overwrite anything. An empty file is visible for a moment.
        mode = stat.S_IMODE(os.stat(staged_name, dir_fd=root_fd, follow_symlinks=False).st_mode)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(target_name, flags, mode, dir_fd=root_fd))
        os.replace(staged_name, target_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)

    @staticmethod
    def _keep_existing_mode(staged_name: str, target_name: str, root_fd: int) -> None:
        """The old file is the user's, and so is what it allowed."""
        try:
            mode = stat.S_IMODE(os.stat(target_name, dir_fd=root_fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return
        descriptor = os.open(staged_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        try:
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)

    def prepare_empty(self, authorization: WriterAuthorization) -> JobFailure | None:
        root = self._root(authorization)
        if root is None:
            return _failure(JobFailureFamily.INTERNAL, "invalid_writer_authorization")
        try:
            self._ensure_root(root)
            return None
        except PermissionError:
            return _failure(JobFailureFamily.RESOURCE, "output_permission_denied")
        except OSError as error:
            return _io_failure(error)

    @staticmethod
    def _root(authorization: WriterAuthorization) -> _AuthorizedRoot | None:
        if type(authorization) is not WriterAuthorization or authorization.failure is not None:
            return None
        return authorization.token if type(authorization.token) is _AuthorizedRoot else None

    @staticmethod
    def _name(ordinal: int, stem: str) -> tuple[str | None, JobFailure | None]:
        """The ordinal keeps names unique and keeps ``con`` from being the reserved device name."""
        # A file's own name holds neither of these, so only a hostile adapter gets here.
        if not stem or stem in {".", ".."} or "/" in stem:
            return None, _failure(JobFailureFamily.POLICY, "unsafe_output_name")
        prefix, suffix = f"{ordinal:03d}-", ".md"
        room = _MAX_NAME_BYTES - len(prefix) - len(suffix)
        safe = _NOT_PORTABLE.sub("_", stem).encode("utf-8", "ignore")[:room].decode("utf-8", "ignore")
        return f"{prefix}{safe}{suffix}", None

    @staticmethod
    def _render(document: VisibleContentDocument) -> bytes:
        text = "\n".join(document.fragments)
        lines = [line.strip() for line in text.splitlines()]
        clean_lines = [line for line in lines if line]
        rendered = "\n\n".join(clean_lines) + ("\n" if clean_lines else "")
        return rendered.encode("utf-8")

    @staticmethod
    def _valid_stage_at(directory: int, name: str, expected: bytes) -> bool:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(name, flags, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return False
                data = stream.read(len(expected) + 1)
            return len(data) == len(expected) and hashlib.sha256(data).digest() == hashlib.sha256(expected).digest()
        except OSError:
            return False

    @staticmethod
    def _target_is_safe(root: _AuthorizedRoot, planned: _PlannedTarget) -> bool:
        try:
            if planned.path.is_symlink():
                return False
            if planned.path.resolve(strict=False) != planned.path:
                return False
            return (planned.path.is_relative_to(root.path) and root.path.resolve(strict=False) == root.path
                    and MarkdownDirectoryWriter._root_is_current(root))
        except OSError:
            return False

    @staticmethod
    def _ensure_root_path(root: Path) -> None:
        # Root paths are resolved at authorization. Reject a symlink introduced in
        # any existing component before creating anything or staging a file.
        for component in (*reversed(root.parents), root):
            if component.is_symlink():
                raise OSError(errno.ELOOP, "unsafe output path")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir() or root.resolve(strict=True) != root:
            raise OSError(errno.ELOOP, "unsafe output path")

    @staticmethod
    def _root_is_current(root: _AuthorizedRoot) -> bool:
        try:
            if root.path.is_symlink() or root.path.resolve(strict=False) != root.path:
                return False
            info = root.path.stat()
            return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (root.device, root.inode)
        except OSError:
            return False

    @staticmethod
    def _ensure_root(root: _AuthorizedRoot) -> None:
        MarkdownDirectoryWriter._ensure_root_path(root.path)
        if not MarkdownDirectoryWriter._root_is_current(root):
            raise OSError(errno.ELOOP, "unsafe output path")

    @staticmethod
    def _open_root(root: _AuthorizedRoot) -> int | None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root.path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (root.device, root.inode):
            os.close(descriptor)
            return None
        return descriptor

    @staticmethod
    def _alias_at(source: FormatSource, name: str, directory: int) -> AliasVerdict:
        if type(source) is not LocalFormatSource:
            return AliasVerdict.UNKNOWN
        try:
            source_identity = source.identity()
            target = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return AliasVerdict.DISTINCT
        except (OSError, ValueError):
            return AliasVerdict.UNKNOWN
        if stat.S_ISLNK(target.st_mode):
            return AliasVerdict.UNKNOWN
        return AliasVerdict.SAME if (target.st_dev, target.st_ino) == source_identity else AliasVerdict.DISTINCT


def _failure(family: JobFailureFamily, code: str) -> JobFailure:
    return JobFailure(family, code)


def _io_failure(error: OSError) -> JobFailure:
    if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return _failure(JobFailureFamily.RESOURCE, "output_permission_denied")
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1), errno.EFBIG}:
        return _failure(JobFailureFamily.RESOURCE, "output_resource_limit")
    if error.errno in {errno.ELOOP}:
        return _failure(JobFailureFamily.POLICY, "output_symlink")
    return _failure(JobFailureFamily.RESOURCE, "output_write_failed")


