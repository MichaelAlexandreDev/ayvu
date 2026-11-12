"""Read EPUB book facts for translation without executing either runtime."""
from __future__ import annotations

import posixpath
from pathlib import Path

from ..book_facts import BookFacts, BookPackage
from .archive import EpubArchive, EpubArchiveError, package_title, parse_package_for_edit


def read_package(path: Path) -> BookPackage:
    """A book the adapter refuses has no facts here, and the engine reports the refusal itself."""
    try:
        with path.open("rb") as stream, EpubArchive(stream) as archive:
            package = archive.package
            title = package_title(parse_package_for_edit(archive.read(package.archive_path)))
            names = tuple((item.archive_path, posixpath.relpath(item.archive_path, package.base or "."))
                          for item in package.documents)
            return BookPackage(BookFacts(names, title is not None and len(title) > 0), package.archive_path, package.language)
    except (EpubArchiveError, OSError, ValueError):
        return BookPackage(BookFacts())
