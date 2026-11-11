"""The local book library and its use cases. Filesystem discovery and the reader launcher are the adapter module library.adapters."""
from __future__ import annotations

from .service import Book, DescribeRequest, LibraryService, ListRequest, OpenRequest, ScanRequest

__all__ = [
    "Book", "DescribeRequest", "LibraryService", "ListRequest", "OpenRequest", "ScanRequest",
]
