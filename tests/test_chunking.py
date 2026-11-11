from ayvu.translation.chunking import split_text


def test_split_text_keeps_short_text_unchanged():
    assert split_text("short text", limit=20) == ["short text"]


def test_split_text_splits_without_losing_order():
    text = "First sentence. Second sentence. Third sentence."
    chunks = split_text(text, limit=25)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 25 for chunk in chunks)


def test_split_text_avoids_cutting_words_when_possible():
    chunks = split_text("alpha beta gamma delta", limit=12)
    assert chunks == ["alpha beta", "gamma delta"]

