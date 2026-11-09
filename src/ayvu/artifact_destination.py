"""Publishes a job's output to the file the user chose, instead of into the project.

The artifact protocol is unchanged: the record is reserved, the bytes are staged and measured, and
only then do they become visible. Only the place differs. The temporary file sits next to the
destination, so publishing is one atomic step on one filesystem, and a journal in the project says
where it went, because the artifact record derives every location from its ID and holds no path.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO

from .artifact import (
    MAX_ARTIFACT_BYTES, Artifact, ArtifactConflictError, ArtifactError, ArtifactIntegrityError, ArtifactNotFoundError,
    ArtifactPathError, ArtifactRequest, ArtifactState, ArtifactStoreError, ArtifactValidationError, validate_identity,
)
from .artifact_service import ArtifactService, PayloadEvidence
from .artifact_store import ArtifactFileOps, _translated_errors
from .format_local import LocalOutputGuard
from .format_service import FormatPublication
from .job_execution import CancellationToken

_READ_CHUNK = 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024
_JOURNAL_VERSION = 1
# What a filesystem without hard links answers. Anything else is a real failure and is reported.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK})


class DestinationFileOps(ArtifactFileOps):
    """The output is an ordinary file of the user, so it follows the umask and not the project's ``0600``."""

    def open_new(self, path: Path) -> BinaryIO:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        return os.fdopen(os.open(path, flags, 0o666), "wb")

    def reserve_name(self, path: Path, mode: int) -> None:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode))

    def replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def chmod(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)


def _hash(stream: BinaryIO) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    while chunk := stream.read(_READ_CHUNK):
        size += len(chunk)
        hasher.update(chunk)
    return size, f"sha256:{hasher.hexdigest()}"


def _open_regular(path: Path) -> tuple[BinaryIO, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ArtifactPathError("symlink_path") from None
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ArtifactPathError("not_a_regular_file")
    return os.fdopen(descriptor, "rb"), info


def _staged_name(destination: Path, artifact_id: str) -> Path:
    return destination.parent / f".ayvu-{artifact_id}.part"


class LocalDestinationPayloads:
    """Bound to one destination when publishing. Without one it can only settle what a past run left."""

    def __init__(self, root: Path, *, destination: Path | None = None, replace_existing: bool = False,
                 ops: DestinationFileOps | None = None, before_publish: Callable[[], None] | None = None) -> None:
        self._journals = Path(os.path.abspath(root)) / "publications"
        # A link stays a link and the file it points at receives the output, as a plain write would do.
        self._destination = None if destination is None else Path(os.path.realpath(destination))
        self._replace = replace_existing
        self.on_staged: Callable[[Path], None] | None = None
        self._ops = ops or DestinationFileOps()
        self._publishing = False
        self._before_publish = before_publish

    def __repr__(self) -> str:
        return "LocalDestinationPayloads()"

    def _journal(self, artifact_id: str) -> Path:
        validate_identity(artifact_id)
        return self._journals / f"{artifact_id}.json"

    def _write_journal(self, artifact_id: str, destination: Path) -> None:
        self._journals.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._journals.is_symlink():
            raise ArtifactPathError("symlink_path")
        path = self._journal(artifact_id)
        payload = json.dumps({"version": _JOURNAL_VERSION, "destination": str(destination), "replace": self._replace})
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self._ops.sync_directory(self._journals)

    def _read_journal(self, artifact_id: str) -> tuple[Path, bool] | None:
        opened = _open_regular(self._journal(artifact_id))
        if opened is None:
            return None
        stream, _ = opened
        with stream:
            raw = stream.read(_MAX_JOURNAL_BYTES + 1)
        try:
            data = json.loads(raw)
            destination, replace, version = data["destination"], data["replace"], data["version"]
        except (ValueError, KeyError, TypeError):
            raise ArtifactPathError("invalid_publication_record") from None
        if (version != _JOURNAL_VERSION or not isinstance(destination, str) or type(replace) is not bool
                or len(raw) > _MAX_JOURNAL_BYTES or not Path(destination).is_absolute()):
            raise ArtifactPathError("invalid_publication_record")
        return Path(destination), replace

    def write_staged(self, artifact_id: str, chunks: Iterable[bytes], between_chunks: Callable[[], None]) -> tuple[int, str]:
        destination = self._destination
        if destination is None:
            raise ArtifactStoreError("no_destination")
        staged = _staged_name(destination, artifact_id)
        with _translated_errors():
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Before anything exists out there, so a crash from here on always has a way back to it.
            self._write_journal(artifact_id, destination)
            return self._write(staged, chunks, between_chunks)

    def _write(self, staged: Path, chunks: Iterable[bytes], between_chunks: Callable[[], None]) -> tuple[int, str]:
        try:
            stream = self._ops.open_new(staged)
        except FileExistsError:
            raise ArtifactConflictError("staging_path_exists") from None
        hasher = hashlib.sha256()
        size = 0
        with stream:
            for chunk in chunks:
                if type(chunk) is not bytes:
                    raise ArtifactValidationError("invalid_chunk")
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise ArtifactValidationError("artifact_too_large")
                hasher.update(chunk)
                self._ops.write(stream, chunk)
                between_chunks()
            self._ops.sync(stream)
        self._ops.sync_directory(staged.parent)
        digest = f"sha256:{hasher.hexdigest()}"
        opened = _open_regular(staged)
        if opened is None:
            raise ArtifactIntegrityError("staged_payload_mismatch")
        reader, info = opened
        with reader:
            if _hash(reader) != (size, digest) or info.st_nlink != 1:
                raise ArtifactIntegrityError("staged_payload_mismatch")
        if self.on_staged is not None:
            self.on_staged(staged)
        return size, digest

    def publish(self, artifact_id: str, size: int, digest: str) -> None:
        destination = self._destination
        if destination is None:
            raise ArtifactStoreError("no_destination")
        staged = _staged_name(destination, artifact_id)
        self._publishing = True
        with _translated_errors():
            if self._replace:
                self._swap(staged, destination)
            else:
                self._link(staged, destination)
            self._ops.sync_directory(destination.parent)
            opened = _open_regular(destination)
            if opened is None:
                raise ArtifactIntegrityError("final_payload_mismatch")
            reader, _ = opened
            with reader:
                if _hash(reader) != (size, digest):
                    raise ArtifactIntegrityError("final_payload_mismatch")
            if os.path.lexists(staged):
                self._ops.unlink(staged)
                self._ops.sync_directory(staged.parent)

    def _swap(self, staged: Path, destination: Path) -> None:
        try:
            mode = stat.S_IMODE(os.lstat(destination).st_mode)
        except FileNotFoundError:
            mode = None
        if mode is not None:
            # The old file is the user's, and so is what it allowed.
            self._ops.chmod(staged, mode)
        if self._before_publish is not None:
            self._before_publish()
        self._ops.replace(staged, destination)

    def _link(self, staged: Path, destination: Path) -> None:
        try:
            if self._before_publish is not None:
                self._before_publish()
            self._ops.link(staged, destination)
            return
        except FileExistsError:
            raise ArtifactConflictError("output_exists") from None
        except OSError as error:
            if error.errno not in _NO_HARD_LINKS:
                raise
        # An exclusive create takes the name, so this still cannot overwrite anything. The price is that
        # an empty file is visible for a moment, and a crash in that moment leaves it there: Ayvu never
        # removes a file at the destination, because it cannot tell it from one the user put there.
        try:
            mode = stat.S_IMODE(os.lstat(staged).st_mode)
            if self._before_publish is not None:
                self._before_publish()
            self._ops.reserve_name(destination, mode)
        except FileExistsError:
            raise ArtifactConflictError("output_exists") from None
        self._ops.replace(staged, destination)

    def evidence(self, artifact_id: str, size: int | None, digest: str | None) -> PayloadEvidence:
        with _translated_errors():
            journal = self._read_journal(artifact_id)
            if journal is None:
                return PayloadEvidence("absent")
            destination, _ = journal
            opened = _open_regular(destination)
            if opened is None:
                return PayloadEvidence("absent")
            stream, info = opened
            with stream:
                measured = _hash(stream)
            if measured != (size, digest):
                return PayloadEvidence("mismatch")
            if info.st_nlink == 1:
                return PayloadEvidence("match")
            return PayloadEvidence("match" if self._is_interrupted_link(_staged_name(destination, artifact_id), info) else "mismatch")

    @staticmethod
    def _is_interrupted_link(staged: Path, final: os.stat_result) -> bool:
        other = _open_regular(staged)
        if other is None:
            return False
        other[0].close()
        return final.st_nlink == 2 and (other[1].st_dev, other[1].st_ino) == (final.st_dev, final.st_ino)

    def complete_publication(self, artifact_id: str) -> None:
        with _translated_errors():
            journal = self._read_journal(artifact_id)
            if journal is None:
                return
            # The final file already matched, so this name holds nothing the output needs, and it is ours.
            staged = _staged_name(journal[0], artifact_id)
            if os.path.lexists(staged):
                self._ops.unlink(staged)
                self._ops.sync_directory(staged.parent)

    def open_verified(self, artifact_id: str, size: int, digest: str) -> BinaryIO:
        with _translated_errors():
            journal = self._read_journal(artifact_id)
            destination = self._destination if journal is None else journal[0]
            opened = None if destination is None else _open_regular(destination)
            if opened is None:
                raise ArtifactIntegrityError("payload_missing")
            stream, _ = opened
            try:
                if _hash(stream) != (size, digest):
                    raise ArtifactIntegrityError("payload_mismatch")
                stream.seek(0)
                return stream
            except BaseException:
                stream.close()
                raise

    @property
    def publishing(self) -> bool:
        """From here on the file may be visible, so only reconciliation may clean up."""
        return self._publishing

    def journal_ids(self) -> tuple[str, ...]:
        with _translated_errors():
            try:
                names = sorted(os.listdir(self._journals))
            except FileNotFoundError:
                return ()
        return tuple(name[:-len(".json")] for name in names if name.endswith(".json"))

    def forget(self, artifact_id: str) -> None:
        """The record has settled, so nothing will need the way back to the file."""
        with _translated_errors():
            try:
                self._ops.unlink(self._journal(artifact_id))
            except FileNotFoundError:
                return
            self._ops.sync_directory(self._journals)

    def discard(self, artifact_id: str) -> None:
        """Removes what an unpublished attempt left next to the destination. Only ever Ayvu's own names."""
        with _translated_errors():
            try:
                journal = self._read_journal(artifact_id)
            except ArtifactPathError:
                journal = None
            if journal is not None:
                staged = _staged_name(journal[0], artifact_id)
                if os.path.lexists(staged):
                    self._ops.unlink(staged)
            self.forget(artifact_id)


class DestinationPublications:
    """One job's output on its way to its destination, and what past runs left behind in this project."""

    def __init__(self, artifacts: ArtifactService, payloads: LocalDestinationPayloads, destination: Path) -> None:
        self._artifacts = artifacts
        self._payloads = payloads
        self._guard = LocalOutputGuard(destination)

    def __repr__(self) -> str:
        return "DestinationPublications()"

    def publication(self, request: ArtifactRequest, owner_token: str, on_staged: Callable[[object], None]) -> FormatPublication:
        self._payloads.on_staged = on_staged
        return FormatPublication(self, request, owner_token, self._guard)

    def publish(self, request: ArtifactRequest, chunks: Iterable[bytes], *, owner_token: str,
                cancellation: CancellationToken | None = None) -> Artifact:
        try:
            artifact = self._artifacts.publish(request, chunks, owner_token=owner_token, cancellation=cancellation)
        except BaseException:
            # A cancellation after staging lands here. The record is abandoned and the file is still ours.
            if not self._payloads.publishing:
                self._settle_journals()
            raise
        self._payloads.forget(artifact.artifact_id)
        return artifact

    def find_ready(self, request: ArtifactRequest) -> Artifact | None:
        return self._artifacts.find_ready(request)

    def reconcile(self) -> None:
        self._artifacts.reconcile()
        self._settle_journals()

    def _settle_journals(self) -> None:
        for artifact_id in self._payloads.journal_ids():
            try:
                state = self._artifacts.get(artifact_id).state
            except ArtifactNotFoundError:
                state = ArtifactState.ABANDONED
            except ArtifactError:
                continue
            if state is ArtifactState.ABANDONED:
                self._payloads.discard(artifact_id)
            elif state is ArtifactState.READY:
                self._payloads.forget(artifact_id)
