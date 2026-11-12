from pathlib import Path


from ayvu.formats.epub.io import detect_epub_language, normalize_language_code



def test_normalize_language_code_extracts_primary_subtag_from_bcp47():
    assert normalize_language_code("pt-BR") == "pt"
    assert normalize_language_code("en_US") == "en"
    assert normalize_language_code("EN") == "en"
    assert normalize_language_code("  fr ") == "fr"


def test_normalize_language_code_rejects_empty_or_invalid_values():
    assert normalize_language_code(None) is None
    assert normalize_language_code("") is None
    assert normalize_language_code("   ") is None
    assert normalize_language_code("english") is None
    assert normalize_language_code("12") is None
    assert normalize_language_code("e n") is None


def test_detect_epub_language_returns_normalized_metadata(minimal_epub_path: Path):
    assert detect_epub_language(minimal_epub_path) == "en"
