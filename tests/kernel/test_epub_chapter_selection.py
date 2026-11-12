
import pytest

from ayvu.kernel import ChapterSelection, ChapterSelectionError



def test_chapter_selection_rejects_invalid_range():
    with pytest.raises(ChapterSelectionError, match="chapter range is reversed"):
        ChapterSelection.parse("3-1")
