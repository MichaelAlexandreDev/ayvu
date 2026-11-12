from pathlib import Path


from ayvu.v1.cache import TranslationCache
from ayvu.formats.epub.io import inspect_epub
from ayvu.v1.epub import translate_epub
from ayvu.kernel import LanguagePair, TranslationOptions

from tests.support.epub_io_support import PrefixTranslator
from tests.support.epub_io_support import _read_chapter_one, _read_navigation_document


def test_translate_epub_preserves_metadata_and_navigation_by_default(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    assert inspect_epub(output_path).title == "Minimal Test Book"
    assert ("Minimal Test Book", "en", "pt") not in translator.calls
    navigation = _read_navigation_document(output_path)
    assert "Chapter One" in navigation
    assert "PT:Chapter One" not in navigation


def test_translate_epub_translates_metadata_and_navigation_when_enabled(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        translate_metadata=True,
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    assert inspect_epub(output_path).title == "PT:Minimal Test Book"
    assert ("Minimal Test Book", "en", "pt") in translator.calls
    navigation = _read_navigation_document(output_path)
    assert 'PT:<a href="text/chapter1.xhtml">Chapter One</a>' in navigation


def test_translate_epub_collects_metadata_review_segment_when_enabled(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        translate_metadata=True,
    )
    review_segments = []

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
            review_segments=review_segments,
        )

    metadata = next(segment for segment in review_segments if segment.segment_id == "metadata-title")
    assert metadata.chapter_index == 0
    assert metadata.chapter_name == "metadata"
    assert metadata.segment_kind == "metadata_title"
    assert metadata.original == "Minimal Test Book"
    assert metadata.translated == "PT:Minimal Test Book"


def test_translate_epub_preserves_image_alt_by_default(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    chapter = _read_chapter_one(output_path)
    assert 'alt="Pixel"' in chapter
    assert ("Pixel", "en", "pt") not in translator.calls
    assert report.alt_texts_translated == 0


def test_translate_epub_translates_image_alt_when_enabled(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        translate_alt_text=True,
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    chapter = _read_chapter_one(output_path)
    # The alt text is translated while the image and its src are preserved.
    assert 'alt="PT:Pixel"' in chapter
    assert "../images/pixel.png" in chapter
    assert ("Pixel", "en", "pt") in translator.calls
    assert report.alt_texts_translated == 1
