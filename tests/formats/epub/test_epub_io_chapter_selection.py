from pathlib import Path

import pytest

from ayvu.formats.epub.io import resolve_chapter_selection
from ayvu.kernel import ChapterSelection, ChapterSelectionError



def test_resolve_chapter_selection_matches_title_and_path_patterns(minimal_epub_path: Path):
    by_title = resolve_chapter_selection(minimal_epub_path, ChapterSelection.parse("Chapter Two"))
    by_path = resolve_chapter_selection(minimal_epub_path, ChapterSelection.parse("*chapter2*"))

    assert [chapter.index for chapter in by_title] == [2]
    assert by_title[0].title == "Chapter Two"
    assert [chapter.index for chapter in by_path] == [2]


def test_resolve_chapter_selection_rejects_unmatched_selection(minimal_epub_path: Path):
    with pytest.raises(ChapterSelectionError, match="no chapters matched selection"):
        resolve_chapter_selection(minimal_epub_path, ChapterSelection.parse("999"))
