"""Explicit local composition. No automatic migration or pipeline integration."""
from pathlib import Path

from ...glossary_catalog_store import ManagedGlossaryRepository
from ...projects.profiles.glossary_store import LegacyGlossaryResolver
from ...projects.profiles import ProjectProfileService
from ...projects.profiles.store import JsonProjectProfileRepository
from ...projects.store import ProjectJsonRepository


def project_profile_service(processing_root: Path, managed_glossary_root: Path) -> ProjectProfileService:
    return ProjectProfileService(JsonProjectProfileRepository(ProjectJsonRepository(processing_root)),
                                 LegacyGlossaryResolver(ManagedGlossaryRepository(managed_glossary_root)))
