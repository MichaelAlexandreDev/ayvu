"""Framework-neutral project resolution and repository contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .project import ProjectConflictError, ProjectManifest, ProjectRequest, ProjectSourceError


@dataclass(frozen=True)
class ProjectResolution:
    project: ProjectManifest
    created: bool
    reason: str = "reused"


class ProjectRepository(Protocol):
    """Implementations serialize resolve/create/update against the same store."""

    def resolve(self, request: ProjectRequest) -> ProjectResolution: ...
    def create(self, request: ProjectRequest) -> ProjectManifest: ...
    def load(self, project_id: str) -> ProjectManifest: ...
    def save(self, project: ProjectManifest, *, expected_revision: int) -> ProjectManifest: ...


def select_project(projects: tuple[ProjectManifest, ...], request: ProjectRequest) -> ProjectManifest | None:
    compatible = tuple(project for project in projects if project.matches(request))
    if len(compatible) > 1:
        raise ProjectConflictError("ambiguous_project")
    if compatible:
        return compatible[0]
    same_source = tuple(project for project in projects if project.source_path == request.source_path)
    if any(project.source_sha256 != request.source_sha256 or project.source_size != request.source_size
           for project in same_source):
        raise ProjectSourceError("source_changed")
    # A new language/configuration is a new intention. Never merge it with an
    # incompatible manifest; the adapter reports a distinct creation outcome.
    return None


def resolve_project(repository: ProjectRepository, request: ProjectRequest) -> ProjectResolution:
    return repository.resolve(request)
