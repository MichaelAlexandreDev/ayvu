"""Compose the managed glossary adapter from the local session directory."""
from pathlib import Path

from ...knowledge.glossary.catalog_store import ManagedGlossaryRepository


def managed_glossary_repository(directory: Path) -> ManagedGlossaryRepository:
    return ManagedGlossaryRepository(directory)
