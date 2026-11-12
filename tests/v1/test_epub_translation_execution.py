import threading
from pathlib import Path
from zipfile import ZipFile


from ayvu.v1.cache import TranslationCache
from ayvu.v1.epub import translate_epub
from ayvu.kernel import LanguagePair, TranslationOptions

from tests.support.epub_io_support import PrefixTranslator
from tests.support.epub_io_support import RaisingTranslator


def test_translate_epub_translates_minimal_generated_epub_without_mutating_input(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    original_bytes = minimal_epub_path.read_bytes()
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    assert output_path.exists()
    assert minimal_epub_path.read_bytes() == original_bytes
    assert report.input_path == minimal_epub_path
    assert report.output_path == output_path
    assert report.detected_language == "en"
    assert report.target_language == "pt"
    assert report.chapters_processed >= 2
    assert report.texts_translated >= 4
    assert report.errors == []
    assert translator.calls

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter = output_epub.read(chapter_name).decode("utf-8")
        assert any(name.endswith("images/pixel.png") for name in names)

    assert "PT:Hello reader. Visit" in chapter
    # The paragraph is translated as one block, keeping the inline link and its text.
    assert '<a href="chapter2.xhtml#answer">chapter two</a>' in chapter
    assert "../images/pixel.png" in chapter


def test_translate_epub_parallel_workers_preserves_order_and_review_segments(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    cache_path = tmp_path / "cache.sqlite"
    translator_ids: list[int] = []
    translator_lock = threading.Lock()
    review_segments = []
    statuses: list[str] = []
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        workers=2,
    )

    def translator_factory() -> PrefixTranslator:
        with translator_lock:
            translator_ids.append(len(translator_ids) + 1)
        return PrefixTranslator()

    with TranslationCache(cache_path) as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=PrefixTranslator(),
            cache=cache,
            options=options,
            review_segments=review_segments,
            on_text_processed=statuses.append,
            translator_factory=translator_factory,
            cache_factory=lambda: TranslationCache(cache_path),
        )

    assert output_path.exists()
    assert report.errors == []
    assert report.chapters_processed >= 2
    assert len(translator_ids) == report.chapters_processed
    assert statuses
    assert [segment.chapter_index for segment in review_segments] == sorted(
        segment.chapter_index for segment in review_segments
    )
    assert {segment.chapter_index for segment in review_segments} >= {1, 2}

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_one_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter_two_name = next(name for name in names if name.endswith("text/chapter2.xhtml"))
        chapter_one = output_epub.read(chapter_one_name).decode("utf-8")
        chapter_two = output_epub.read(chapter_two_name).decode("utf-8")

    assert "PT:Hello reader. Visit" in chapter_one
    assert "PT:Goodbye reader." in chapter_two


def test_translate_epub_cache_only_reuses_cache_without_calling_translator(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    cache_path = tmp_path / "cache.sqlite"
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    with TranslationCache(cache_path) as cache:
        first = translate_epub(
            minimal_epub_path,
            tmp_path / "first-pt.epub",
            translator=PrefixTranslator(),
            cache=cache,
            options=options,
        )

    output_path = tmp_path / "cache-only-pt.epub"
    raising = RaisingTranslator()
    cache_only_options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        cache_only=True,
    )
    with TranslationCache(cache_path) as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=raising,
            cache=cache,
            options=cache_only_options,
        )

    assert output_path.exists()
    assert report.output_written
    assert report.texts_missing == 0
    assert report.texts_translated == 0
    assert report.texts_from_cache == first.texts_translated + first.texts_from_cache
    assert raising.calls == []

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter = output_epub.read(chapter_name).decode("utf-8")
    assert "PT:Hello reader. Visit" in chapter


def test_translate_epub_cache_only_writes_with_missing_texts_by_default(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "cache-only-pt.epub"
    raising = RaisingTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        cache_only=True,
    )
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=raising,
            cache=cache,
            options=options,
        )

    assert output_path.exists()
    assert report.output_written
    assert report.texts_missing >= 4
    assert report.texts_translated == 0
    assert report.texts_from_cache == 0
    assert len(report.missing_texts) == report.texts_missing
    assert raising.calls == []

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter = output_epub.read(chapter_name).decode("utf-8")
    assert "Hello reader. Visit" in chapter
    assert "PT:" not in chapter


def test_translate_epub_cache_only_require_full_cache_blocks_output_when_missing(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "cache-only-pt.epub"
    raising = RaisingTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        cache_only=True,
        require_full_cache=True,
    )
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=raising,
            cache=cache,
            options=options,
        )

    assert not output_path.exists()
    assert not report.output_written
    assert report.texts_missing >= 4
    assert raising.calls == []


def test_translate_epub_collects_review_segments_with_document_metadata(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
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

    assert review_segments
    assert review_segments[0].segment_id == "c0001-s0001"
    assert review_segments[0].source_epub == str(minimal_epub_path)
    assert review_segments[0].output_epub == str(output_path)
    assert review_segments[0].chapter_index == 1
    assert review_segments[0].document_path.endswith("text/chapter1.xhtml")
    assert review_segments[0].source_language == "en"
    assert review_segments[0].target_language == "pt"

    paragraph = next(segment for segment in review_segments if "Hello reader" in segment.original)
    assert paragraph.segment_id == "c0001-s0003"
    assert paragraph.segment_kind == "text"
    assert paragraph.original == "Hello reader. Visit chapter two."
    assert paragraph.translated == "PT:Hello reader. Visit chapter two."
    assert not paragraph.from_cache
