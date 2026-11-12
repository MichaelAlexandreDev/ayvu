from pathlib import Path
from zipfile import ZipFile


from ayvu.v1.cache import TranslationCache
from ayvu.v1.epub import translate_epub
from ayvu.knowledge.glossary.rules import GLOSSARY_RULE_FORBIDDEN, GLOSSARY_RULE_PRESERVE, Glossary, GlossaryEntry
from ayvu.kernel import ChapterSelection, LanguagePair, TranslationOptions

from tests.support.epub_io_support import PrefixTranslator


def test_translate_epub_limits_documents_for_preview(minimal_epub_path: Path, tmp_path: Path):
    output_path = tmp_path / "minimal-preview.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        max_documents=1,
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    assert report.chapters_processed == 1

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_one_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter_two_name = next(name for name in names if name.endswith("text/chapter2.xhtml"))
        chapter_one = output_epub.read(chapter_one_name).decode("utf-8")
        chapter_two = output_epub.read(chapter_two_name).decode("utf-8")

    assert "PT:Hello reader. Visit" in chapter_one
    assert "PT:Goodbye reader." not in chapter_two
    assert "Goodbye reader." in chapter_two


def test_translate_epub_translates_selected_chapter_by_index_without_changing_others(
    minimal_epub_path: Path,
    tmp_path: Path,
):
    output_path = tmp_path / "minimal-selected.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(
        language_pair=LanguagePair(source="en", target="pt"),
        chapter_selection=ChapterSelection.parse("2"),
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
        )

    assert report.chapters_processed == 1

    with ZipFile(output_path) as output_epub:
        names = output_epub.namelist()
        chapter_one_name = next(name for name in names if name.endswith("text/chapter1.xhtml"))
        chapter_two_name = next(name for name in names if name.endswith("text/chapter2.xhtml"))
        chapter_one = output_epub.read(chapter_one_name).decode("utf-8")
        chapter_two = output_epub.read(chapter_two_name).decode("utf-8")

    assert "PT:Hello reader. Visit" not in chapter_one
    assert "Hello reader. Visit" in chapter_one
    assert "PT:Goodbye reader." in chapter_two


def test_translate_epub_reports_glossary_usage(minimal_epub_path: Path, tmp_path: Path):
    output_path = tmp_path / "minimal-pt.epub"
    translator = PrefixTranslator()
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    glossary = Glossary(
        [
            GlossaryEntry("PT:Hello reader", GLOSSARY_RULE_PRESERVE, required=True),
            GlossaryEntry("Missing Term", GLOSSARY_RULE_PRESERVE, required=True),
            GlossaryEntry("PT:Chapter Two", GLOSSARY_RULE_FORBIDDEN),
        ]
    )

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(
            minimal_epub_path,
            output_path,
            translator=translator,
            cache=cache,
            options=options,
            glossary=glossary,
        )

    assert report.glossary_terms_configured == 3
    assert report.glossary_usage.applied_terms["PT:Hello reader"] == 1
    assert report.glossary_usage.required_terms_present["PT:Hello reader"] == 1
    assert report.glossary_usage.required_terms_missing == ["Missing Term"]
    assert report.glossary_usage.forbidden_terms_found["PT:Chapter Two"] >= 1
