"""Facts about a book that its documents do not say, read by format adapters."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class BookFacts:
    """What the application needs about a book that its documents do not say."""

    document_names: tuple[tuple[str, str], ...] = ()
    title_holds_other_nodes: bool = False

    def __repr__(self) -> str:
        return f"BookFacts(documents={len(self.document_names)})"


@dataclass(frozen=True, repr=False)
class BookPackage:
    facts: BookFacts
    archive_path: str | None = None
    language: str | None = None
