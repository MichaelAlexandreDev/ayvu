"""The local book library: discovery, description and opening a book in a reader."""
from __future__ import annotations

from .adapters import LocalLibraryFilesystem, ReaderLauncher
from .service import Book, DescribeRequest, LibraryService, ListRequest, OpenRequest, ScanRequest

__all__ = [
    "Book", "DescribeRequest", "LibraryService", "ListRequest", "LocalLibraryFilesystem", "OpenRequest",
    "ReaderLauncher", "ScanRequest",
]
