"""Framework-neutral, immutable project manifest values."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID

PROJECT_SCHEMA_VERSION = 1
MAX_PROJECT_METADATA_ITEMS = 32
OPAQUE_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}")
LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,34}")


class ProjectError(ValueError):
    """A bounded error: messages never include caller data."""


class ProjectSourceError(ProjectError):
    pass


class ProjectConflictError(ProjectError):
    pass


class ProjectNotFoundError(ProjectError):
    pass


class ProjectManifestError(ProjectError):
    pass


def validate_project_reference(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not OPAQUE_REFERENCE.fullmatch(value)):
        raise ProjectManifestError("invalid_reference")


def validate_locations(locations: tuple[str, ...]) -> None:
    if type(locations) is not tuple or len(locations) > 16:
        raise ProjectManifestError("invalid_locations")
    seen: set[str] = set()
    for value in locations:
        if (not isinstance(value, str) or not value or len(value) > 240
                or "\\" in value or ":" in value or "\x00" in value
                or PurePosixPath(value).is_absolute()
                or any(part in {"", ".", ".."} for part in value.split("/"))
                or any(part.endswith(('.', ' ')) for part in value.split('/'))
                or any(ord(character) < 32 for character in value)
                or value.casefold() in seen):
            raise ProjectManifestError("invalid_locations")
        seen.add(value.casefold())


@dataclass(frozen=True, repr=False)
class ProjectRequest:
    """Already fingerprinted source and trusted compatibility selections."""

    source_path: str
    source_size: int
    source_sha256: str
    source: str
    target: str
    format_id: str = "epub"
    profile_ref: str | None = None
    provider_ref: str | None = None
    policy_ref: str | None = None
    glossary_ref: str | None = None
    configuration_ref: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.source_path, str) or not self.source_path
                or len(self.source_path) > 4096 or "\x00" in self.source_path):
            raise ProjectSourceError("invalid_source_reference")
        if not (PurePosixPath(self.source_path).is_absolute()
                or PureWindowsPath(self.source_path).is_absolute()):
            raise ProjectSourceError("invalid_source_reference")
        if (type(self.source_size) is not int or not 0 <= self.source_size <= 4 * 1024**3
                or not isinstance(self.source_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256)):
            raise ProjectSourceError("invalid_source_fingerprint")
        if any(not isinstance(x, str) or not LANGUAGE.fullmatch(x) for x in (self.source, self.target)):
            raise ProjectManifestError("invalid_language")
        if self.format_id != "epub":
            raise ProjectManifestError("unsupported_format")
        for value in (self.profile_ref, self.provider_ref, self.policy_ref,
                      self.glossary_ref, self.configuration_ref):
            validate_project_reference(value)

    def __repr__(self) -> str:
        return "ProjectRequest(<redacted>)"

    def compatibility_key(self) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in self.__dataclass_fields__)


@dataclass(frozen=True, repr=False)
class ProjectManifest:
    project_id: str
    schema_version: int
    revision: int
    source_path: str
    source_size: int
    source_sha256: str
    source: str
    target: str
    format_id: str
    profile_ref: str | None
    provider_ref: str | None
    policy_ref: str | None
    glossary_ref: str | None
    created_at: str
    updated_at: str
    configuration_ref: str | None = None
    locations: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    def __repr__(self) -> str:
        return f"ProjectManifest(project_id={self.project_id!r}, revision={self.revision})"

    @classmethod
    def create(cls, request: ProjectRequest, project_id: str) -> ProjectManifest:
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        return cls(project_id=project_id, schema_version=PROJECT_SCHEMA_VERSION,
                   revision=1, created_at=now, updated_at=now,
                   **{name: getattr(request, name) for name in request.__dataclass_fields__})

    def validate(self) -> None:
        try:
            if not isinstance(self.project_id, str) or str(UUID(self.project_id)) != self.project_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ProjectManifestError("invalid_project_identity") from None
        if type(self.schema_version) is not int or self.schema_version != PROJECT_SCHEMA_VERSION:
            raise ProjectManifestError("unsupported_schema")
        if type(self.revision) is not int or not 1 <= self.revision < 2**63:
            raise ProjectManifestError("invalid_revision")
        self.request()
        stamps = []
        for value in (self.created_at, self.updated_at):
            try:
                if not isinstance(value, str) or len(value) > 40:
                    raise ValueError
                parsed = datetime.fromisoformat(value)
                if parsed.utcoffset() is None:
                    raise ValueError
                stamps.append(parsed)
            except (ValueError, TypeError):
                raise ProjectManifestError("invalid_timestamp") from None
        if stamps[1] < stamps[0]:
            raise ProjectManifestError("invalid_timestamp_order")
        validate_locations(self.locations)
        if type(self.metadata) is not tuple or len(self.metadata) > MAX_PROJECT_METADATA_ITEMS:
            raise ProjectManifestError("invalid_metadata")
        seen = set()
        for entry in self.metadata:
            if type(entry) is not tuple or len(entry) != 2:
                raise ProjectManifestError("invalid_metadata")
            key, value = entry
            if (not isinstance(key, str) or not re.fullmatch(r"x-[a-z0-9-]{1,30}", key)
                    or key in seen):
                raise ProjectManifestError("invalid_metadata")
            validate_project_reference(value)
            if value is None:
                raise ProjectManifestError("invalid_metadata")
            seen.add(key)

    def matches(self, request: ProjectRequest) -> bool:
        return self.request().compatibility_key() == request.compatibility_key()

    def request(self) -> ProjectRequest:
        return ProjectRequest(**{name: getattr(self, name) for name in ProjectRequest.__dataclass_fields__})
