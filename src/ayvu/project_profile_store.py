"""Append-oriented private profile records, published by #134 manifest CAS.

One content-addressed state file is staged before an atomic manifest replacement.
Unreferenced staging records are never a published profile/snapshot. The manifest
and its state are read under the same workspace lock as ordinary project updates.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

from .project import ProjectError
from .project_store import ProjectJsonRepository, _read_regular, _safe_components, encode_manifest
from .project_profile_service import ProjectProfileState, check_cancel
from .project_profiles import ProfileError, ProjectReference, digest

STATE_KEY = 'x-translation-profiles'
MAX_STATE_BYTES = 8 * 1024 * 1024


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError('invalid_profile')
        result[key] = value
    return result


def _constant(value):
    raise ProfileError('invalid_profile')


def encode_state(state: ProjectProfileState) -> bytes:
    payload = (json.dumps(state.to_mapping(), sort_keys=True, ensure_ascii=True,
                          separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
    if len(payload) > MAX_STATE_BYTES:
        raise ProfileError('limit')
    return payload


def decode_state(payload: bytes) -> ProjectProfileState:
    if type(payload) is not bytes or len(payload) > MAX_STATE_BYTES:
        raise ProfileError('limit')
    try:
        return ProjectProfileState.from_mapping(json.loads(payload, object_pairs_hook=_unique, parse_constant=_constant))
    except ProfileError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        raise ProfileError('invalid_profile') from None


class JsonProjectProfileRepository:
    def __init__(self, projects: ProjectJsonRepository):
        self.projects = projects

    def _load(self, project):
        if type(project) is not ProjectReference:
            raise ProfileError('invalid_profile')
        manifest = self.projects._load(project.value)
        pointer = dict(manifest.metadata).get(STATE_KEY)
        if pointer is None:
            return manifest, ProjectProfileState(project, manifest.revision)
        # #134 already validates opaque-reference syntax; this namespace is closed.
        if not pointer.startswith('profiles:'):
            raise ProfileError('invalid_profile')
        root = self.projects._manifest_path(project.value).parent / 'profiles'
        payload = _read_regular(root / (pointer.split(':')[1] + '.json'), MAX_STATE_BYTES)
        state = decode_state(payload)
        if (digest(state.to_mapping()) != pointer.split(':')[1] or state.project != project
                or state.project_revision > manifest.revision):
            raise ProfileError('invalid_profile')
        # Unrelated #134 updates can advance the manifest without editing profiles.
        return manifest, replace(state, project_revision=manifest.revision)

    def read(self, project):
        try:
            with self.projects._transaction():
                return self._load(project)[1]
        except ProfileError:
            raise
        except (ProjectError, OSError):
            raise ProfileError('persistence') from None

    @staticmethod
    def _temporary(path, data, mode=0o600):
        fd, name = tempfile.mkstemp(prefix='.profile-', dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                if os.name != 'nt':
                    os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return temp
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def _publish_manifest(self, path, manifest, cancel, validate):
        """Prepare both files before publication; restore prior bytes on sync error.

        As in #157, a persistently failing filesystem cannot promise rollback
        durability. It never yields success, and the old manifest remains the
        recovery source. No cancellation point follows the publication rename.
        """
        old = _read_regular(path, 32 * 1024)
        mode = path.stat().st_mode & 0o600
        rollback = self._temporary(path, old, mode)
        temp = None
        try:
            temp = self._temporary(path, encode_manifest(manifest), mode)
            self.projects._sync_directory(path.parent)
            validate()
            check_cancel(cancel)
            os.replace(temp, path)
            temp = None
            try:
                self.projects._sync_directory(path.parent)
            except OSError:
                os.replace(rollback, path)
                rollback = None
                raise ProfileError('persistence') from None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if rollback is not None:
                rollback.unlink(missing_ok=True)

    def publish(self, state, expected_revision, cancel, validate):
        try:
            check_cancel(cancel)
            with self.projects._transaction():
                manifest, current = self._load(state.project)
                if (type(expected_revision) is not int or manifest.revision != expected_revision
                        or state.project_revision != expected_revision + 1):
                    raise ProfileError('conflict')
                # History is append-only, even for another application caller.
                if (not set(current.records).issubset(state.records)
                        or not set(current.snapshots).issubset(state.snapshots)
                        or not set(current.migrations).issubset(state.migrations)):
                    raise ProfileError('conflict')
                state.__post_init__()
                payload = encode_state(state)
                pointer = 'profiles:' + digest(state.to_mapping())
                metadata = dict(manifest.metadata)
                metadata[STATE_KEY] = pointer
                updated = replace(manifest, revision=state.project_revision, metadata=tuple(sorted(metadata.items())),
                                  updated_at=datetime.now(UTC).isoformat(timespec='microseconds'))
                self.projects._check_source(manifest.request())
                self.projects._validate_locations(updated)
                root = _safe_components(self.projects._manifest_path(state.project.value).parent / 'profiles')
                root.mkdir(mode=0o700, exist_ok=True)
                path = _safe_components(root / (pointer.split(':')[1] + '.json'))
                if path.exists():
                    if _read_regular(path, MAX_STATE_BYTES) != payload:
                        raise ProfileError('conflict')
                else:
                    temp = self._temporary(path, payload)
                    try:
                        os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
                self.projects._sync_directory(root)
                self._publish_manifest(self.projects._manifest_path(state.project.value), updated, cancel, validate)
                return state
        except ProfileError:
            raise
        except (ProjectError, OSError, ValueError):
            raise ProfileError('persistence') from None
