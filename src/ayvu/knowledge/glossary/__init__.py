"""Glossaries: the rules applied after translation and the managed catalog. The catalog's JSON store is the
adapter module knowledge.glossary.catalog_store.
"""
from __future__ import annotations

from .catalog import (
    Entry, GlossaryReference, Snapshot, _valid_entries, create_glossary, list_glossaries, read_glossary,
    update_glossary,
)
from .rules import (
    Glossary, GlossaryError, GlossaryUsage, apply_glossary_with_usage, effective_glossary_reference,
    load_glossary,
)

__all__ = [
    "Entry", "Glossary", "GlossaryError", "GlossaryReference", "GlossaryUsage", "Snapshot", "_valid_entries",
    "apply_glossary_with_usage", "create_glossary", "effective_glossary_reference", "list_glossaries",
    "load_glossary", "read_glossary", "update_glossary",
]
