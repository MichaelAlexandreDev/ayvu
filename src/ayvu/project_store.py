"""Bounded JSON manifests and an atomic local project repository.

The workspace is private to Ayvu. Locks coordinate cooperating Ayvu processes;
they are not a sandbox against another process running as the same OS user.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from .project import (
    ProjectError, ProjectManifest, ProjectManifestError, ProjectNotFoundError,
    ProjectRequest, ProjectSourceError, ProjectConflictError,
)
from .project_service import ProjectResolution, select_project

MAX_MANIFEST_BYTES = 32 * 1024
MAX_PROJECTS = 1024
MAX_SOURCE_BYTES = 4 * 1024**3
LOCK_TIMEOUT_SECONDS = 5.0


class ProjectStoreError(ProjectError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectManifestError("duplicate_field")
        result[key] = value
    return result


def encode_manifest(project: ProjectManifest) -> bytes:
    project.validate()
    data = asdict(project)
    data["metadata"] = dict(project.metadata)
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ProjectManifestError("manifest_too_large")
    return payload


def decode_manifest(payload: bytes) -> ProjectManifest:
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ProjectManifestError("manifest_too_large")
    try:
        data = json.loads(payload, object_pairs_hook=_no_duplicates)
        if not isinstance(data, dict) or set(data) != set(ProjectManifest.__dataclass_fields__):
            raise ProjectManifestError("unknown_or_missing_fields")
        if not isinstance(data["locations"], list) or not isinstance(data["metadata"], dict):
            raise ProjectManifestError("invalid_manifest")
        data["locations"] = tuple(data["locations"])
        data["metadata"] = tuple(sorted(data["metadata"].items()))
        return ProjectManifest(**data)
    except ProjectError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectManifestError("invalid_manifest") from None


def _safe_components(path: Path) -> Path:
    """Reject symlinks in every existing component before filesystem access."""
    absolute = Path(os.path.abspath(path.expanduser()))
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise ProjectStoreError("symlink_path")
    return absolute


def _read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(_safe_components(path), flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProjectStoreError("unsafe_file")
        if info.st_size > limit:
            raise ProjectStoreError("file_too_large")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ProjectStoreError("file_too_large")
        return data


def _fingerprint(path: Path, limit: int = MAX_SOURCE_BYTES, *, allow_aliases: bool = False) -> tuple[str, int, str]:
    try:
        canonical = _safe_components(path.expanduser().resolve(strict=True) if allow_aliases else path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(canonical, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or (not allow_aliases and before.st_nlink != 1):
                raise ProjectSourceError("source_alias_or_not_regular")
            if before.st_size > limit:
                raise ProjectSourceError("source_too_large")
            digest = hashlib.sha256()
            count = 0
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > limit:
                    raise ProjectSourceError("source_too_large")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            current = _safe_components(canonical).lstat()
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                       value.st_mtime_ns, value.st_ctime_ns)
            if signature(before) != signature(after) or signature(after) != signature(current):
                raise ProjectSourceError("source_changed_during_read")
        return os.path.normcase(str(canonical)), count, digest.hexdigest()
    except FileNotFoundError:
        raise ProjectSourceError("source_missing") from None
    except ProjectError:
        raise
    except (OSError, ValueError):
        raise ProjectSourceError("source_unreadable") from None


def build_project_request(
    source_path: Path, *, source: str, target: str,
    selections: dict[str, object] | None = None, glossary_path: Path | None = None,
    profile_name: str | None = None,
) -> ProjectRequest:
    canonical, size, fingerprint = _fingerprint(source_path)
    try:
        # Raw configuration is transient. Only digests enter the manifest/DTO.
        options = dict(selections or {})
        endpoint = options.get("url")
        if endpoint is not None:
            if not isinstance(endpoint, str):
                raise ProjectManifestError("invalid_provider_endpoint")
            parsed = urlsplit(endpoint)
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ProjectManifestError("credential_bearing_endpoint")
        glossary_ref = None
        if glossary_path is not None:
            _, _, glossary_digest = _fingerprint(glossary_path, 1024 * 1024, allow_aliases=True)
            glossary_ref = "glossary:" + glossary_digest
        options["glossary_ref"] = glossary_ref
        options["profile_name"] = profile_name
        identity = hashlib.sha256(json.dumps(options, sort_keys=True,
                                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        provider = hashlib.sha256(json.dumps(
            [options.get("translator"), options.get("url")], separators=(",", ":")).encode()).hexdigest()
        profile = None if profile_name is None else "profile:" + hashlib.sha256(profile_name.encode()).hexdigest()
        return ProjectRequest(
            canonical, size, fingerprint, source, target, profile_ref=profile,
            glossary_ref=glossary_ref, provider_ref="provider:" + provider,
            configuration_ref="configuration:" + identity,
        )
    except ProjectError:
        raise
    except (ValueError, TypeError):
        raise ProjectManifestError("invalid_configuration") from None


@contextmanager
def _workspace_lock(path: Path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = os.fdopen(os.open(_safe_components(path), flags, 0o600), "r+b")
    locked = False
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProjectStoreError("unsafe_lock")
        if info.st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if os.name == "nt":
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
                    raise ProjectStoreError("workspace_busy") from None
                time.sleep(0.01)
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class ProjectJsonRepository:
    def __init__(self, processing_root: Path) -> None:
        self.root = Path(os.path.abspath(processing_root.expanduser())) / "projects"

    @contextmanager
    def _transaction(self):
        try:
            root = _safe_components(self.root)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                root.chmod(0o700)
            with _workspace_lock(root / ".workspace.lock"):
                yield
        except ProjectError:
            raise
        except (OSError, ValueError):
            raise ProjectStoreError("project_storage_unavailable") from None

    def _manifest_path(self, project_id: str) -> Path:
        from uuid import UUID
        try:
            if not isinstance(project_id, str) or str(UUID(project_id)) != project_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ProjectNotFoundError("invalid_project_identity") from None
        return _safe_components(self.root / project_id / "project.json")

    def _load(self, project_id: str) -> ProjectManifest:
        path = self._manifest_path(project_id)
        try:
            manifest = decode_manifest(_read_regular(path, MAX_MANIFEST_BYTES))
        except FileNotFoundError:
            raise ProjectNotFoundError("manifest_missing") from None
        if manifest.project_id != project_id:
            raise ProjectManifestError("project_identity_mismatch")
        self._validate_locations(manifest)
        return manifest

    def _validate_locations(self, project: ProjectManifest) -> None:
        directory = self._manifest_path(project.project_id).parent
        source = Path(project.source_path)
        if not source.is_absolute():
            raise ProjectManifestError("invalid_source_reference")
        if source.is_relative_to(self.root):
            raise ProjectSourceError("source_inside_workspace")
        targets = {directory, directory / "project.json"}
        for location in project.locations:
            target = _safe_components(directory / location)
            if not target.is_relative_to(directory) or target in targets:
                raise ProjectManifestError("invalid_locations")
            if target == source or (target.exists() and source.exists() and target.samefile(source)):
                raise ProjectManifestError("source_alias")
            # Never accept a hardlinked project-owned file, even from another project.
            if target.exists() and target.stat().st_nlink > 1 and target.is_file():
                raise ProjectManifestError("location_alias")
            targets.add(target)

    def _list(self) -> tuple[ProjectManifest, ...]:
        projects = []
        for count, entry in enumerate(self.root.iterdir()):
            if count > MAX_PROJECTS + 1:
                raise ProjectStoreError("workspace_too_large")
            if entry.name == ".workspace.lock":
                continue
            if entry.name.startswith(".staging-"):
                # A killed creator leaves a recoverable unpublished directory.
                raise ProjectStoreError("incomplete_project_creation")
            if entry.is_symlink() or not entry.is_dir():
                raise ProjectStoreError("invalid_workspace_entry")
            projects.append(self._load(entry.name))
            if len(projects) > MAX_PROJECTS:
                raise ProjectStoreError("workspace_too_large")
        return tuple(projects)

    def list_projects(self) -> tuple[ProjectManifest, ...]:
        with self._transaction():
            return self._list()

    def load(self, project_id: str) -> ProjectManifest:
        with self._transaction():
            return self._load(project_id)

    def _check_source(self, request: ProjectRequest) -> None:
        canonical, size, digest = _fingerprint(Path(request.source_path))
        if Path(canonical).is_relative_to(self.root):
            raise ProjectSourceError("source_inside_workspace")
        if (canonical, size, digest) != (request.source_path, request.source_size, request.source_sha256):
            raise ProjectSourceError("source_changed")

    def resolve(self, request: ProjectRequest) -> ProjectResolution:
        with self._transaction():
            self._check_source(request)
            projects = self._list()
            selected = select_project(projects, request)
            if selected is not None:
                return ProjectResolution(selected, False)
            if len(projects) >= MAX_PROJECTS:
                raise ProjectStoreError("workspace_too_large")
            reason = ("new_configuration" if any(
                project.source_path == request.source_path for project in projects
            ) else "created")
            return ProjectResolution(self._create(request), True, reason)

    def create(self, request: ProjectRequest) -> ProjectManifest:
        # An explicit caller can create a new configuration intentionally.
        # Standalone resolution never uses this to bypass a conflict.
        with self._transaction():
            self._check_source(request)
            projects = self._list()
            if len(projects) >= MAX_PROJECTS:
                raise ProjectStoreError("workspace_too_large")
            if any(project.matches(request) for project in projects):
                raise ProjectConflictError("project_already_exists")
            return self._create(request)

    def _create(self, request: ProjectRequest) -> ProjectManifest:
        manifest = ProjectManifest.create(request, str(uuid4()))
        self._validate_locations(manifest)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            self._write_manifest(staging / "project.json", manifest)
            os.rename(staging, self.root / manifest.project_id)
            try:
                self._sync_directory(self.root)
            except OSError:
                raise ProjectStoreError("project_commit_uncertain") from None
        finally:
            # Clean up only this invocation's unpublished, empty staging directory.
            if staging.exists():
                (staging / "project.json").unlink(missing_ok=True)
                staging.rmdir()
        return manifest

    def save(self, project: ProjectManifest, *, expected_revision: int) -> ProjectManifest:
        with self._transaction():
            current = self._load(project.project_id)
            if type(expected_revision) is not int or current.revision != expected_revision or project.revision != expected_revision:
                raise ProjectConflictError("revision_conflict")
            if project.request() != current.request() or project.created_at != current.created_at:
                raise ProjectConflictError("immutable_project_identity")
            self._check_source(project.request())
            updated = replace(project, revision=expected_revision + 1,
                              updated_at=datetime.now(UTC).isoformat(timespec="microseconds"))
            self._validate_locations(updated)
            self._write_manifest(self._manifest_path(project.project_id), updated)
            return updated

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name != "nt":
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write_manifest(self, path: Path, project: ProjectManifest) -> None:
        encoded = encode_manifest(project)
        _safe_components(path)
        if path.exists() and path.stat().st_nlink != 1:
            raise ProjectStoreError("manifest_alias")
        fd, name = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                self._sync_directory(path.parent)
            except OSError:
                raise ProjectStoreError("project_commit_uncertain") from None
        finally:
            temporary.unlink(missing_ok=True)
