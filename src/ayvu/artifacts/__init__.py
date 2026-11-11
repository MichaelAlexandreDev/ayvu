"""Artifacts a job produces: the record and the service that stages and publishes them. The local stores are the adapter modules artifacts.store and artifacts.destination."""
from __future__ import annotations

from .model import (
    Artifact, ArtifactCancelledError, ArtifactConflictError, ArtifactError, ArtifactIntegrityError,
    ArtifactPathError, ArtifactRequest, ArtifactState, ArtifactStoreError,
)
from .service import ARTIFACT_SAFE_POINTS, ArtifactPayloadStore, ArtifactService

__all__ = [
    "ARTIFACT_SAFE_POINTS", "Artifact", "ArtifactCancelledError", "ArtifactConflictError", "ArtifactError",
    "ArtifactIntegrityError", "ArtifactPathError", "ArtifactPayloadStore", "ArtifactRequest",
    "ArtifactService", "ArtifactState", "ArtifactStoreError",
]
