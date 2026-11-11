"""Project translation profiles: the profile model and its service. The JSON and glossary stores are the
adapter modules projects.profiles.store and projects.profiles.glossary_store.
"""
from __future__ import annotations

from .model import GlossaryStage, ResolvedProfileSnapshot, canonical
from .service import ProjectProfileService

__all__ = [
    "GlossaryStage", "ProjectProfileService", "ResolvedProfileSnapshot", "canonical",
]
