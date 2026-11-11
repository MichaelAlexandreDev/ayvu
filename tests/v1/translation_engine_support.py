from __future__ import annotations
import io
import sqlite3
import threading
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import pytest
from ayvu.v1.cache import TranslationCache
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.v1.epub import translate_epub
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatRegistry, FormatService
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.formats.contracts import MaterializationResult
from ayvu.v1.translation import translate_book
from tests.support.epub_fakes import NAV, OPF, TWO, XHTML, epub_bytes, members, zip_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv
from ayvu.kernel import (ChapterSelection, ChapterSelectionError, LanguagePair, TranslationMemoryOptions,
    TranslationOptions, JobFailure, JobFailureFamily)

PAIR = LanguagePair(source="en", target="pt")


OPF_MEMBER = "OEBPS/content.opf"


# ebooklib, which the current flow opens every book with, rejects a remote manifest item.
OPENABLE_OPF = OPF.replace(b'    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>\n', b"")


# Every document has its own title, because workers sharing a cache can race on a repeated text.
HARD_CHAPTER = XHTML.replace("<title>Doc</title>", "<title>Hard doc</title>").format(
    '<!-- comment --><h1 id="h">Hello <em>big</em> world</h1><p>Text &amp; more &lt;tags&gt; <a epub:type="noteref" href="#h">link</a>.</p>'
    '<p>Run foo_bar and <code>code stays</code> at https://example.test/x now.</p><p>  spaced  <br/>  out  </p><p> </p>'
    '<p>Text that looks __AYVU_TAG_0__ like a token <b>bold</b>.</p><div>Loose <span>inline</span> text<p>inner</p>tail</div>'
    '<img src="../images/a.png" alt="A picture"/><img src="../images/a.png" alt="Another &amp; one"/><pre>pre stays</pre>'
    '<p>café \U0001f600 "quoted" it\'s</p>').encode()


class Prefix:
    def __init__(self, change=None) -> None:
        self.change = change or (lambda text: f"PT:{text}")
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def translate(self, text: str, source: str, target: str) -> str:
        with self.lock:
            self.calls.append(text)
        return self.change(text)


def failing_on(needle: str):
    def change(text: str) -> str:
        if needle in text:
            raise ConnectionError("translator went away")
        return f"PT:{text}"
    return change


@pytest.fixture
def formats():
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def book(tmp_path: Path, *, opf: bytes = OPENABLE_OPF, chapter: bytes = HARD_CHAPTER) -> Path:
    path = tmp_path / "book.epub"
    path.write_bytes(epub_bytes(replace={
        OPF_MEMBER: opf, "OEBPS/text/one.xhtml": chapter, "OEBPS/text/two words.xhtml": TWO.replace(b"<title>Doc</title>", b"<title>Second doc</title>"),
        "OEBPS/nav.xhtml": NAV.replace(b"<title>Doc</title>", b"<title>Nav doc</title>")}))
    return path


def zip_members(data: bytes) -> list[tuple[str, int, tuple, bytes]]:
    with ZipFile(io.BytesIO(data)) as archive:
        return [(info.filename, info.compress_type, info.date_time, archive.read(info)) for info in archive.infolist()]


def cache_rows(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        columns = [row[1] for row in connection.execute("pragma table_info(translations)") if row[1] not in ("id", "created_at", "updated_at")]
        return sorted(connection.execute(f"select {', '.join(columns)} from translations").fetchall(), key=str)
    finally:
        connection.close()


REPORT_FIELDS = (
    "chapters_processed", "texts_translated", "texts_from_cache", "texts_from_memory", "memory_suggestions", "texts_missing",
    "alt_texts_translated", "errors", "missing_texts", "memory_suggestion_texts", "glossary_terms_configured", "output_written",
    "detected_language", "target_language")


def observed(report, translator, statuses, starts, done, output, cache_path) -> dict:
    usage = report.glossary_usage
    return {
        "report": {name: getattr(report, name) for name in REPORT_FIELDS},
        "glossary": (usage.applied_terms, usage.required_terms_missing),
        "calls": sorted(translator.calls) if translator.parallel else translator.calls,
        "statuses": statuses, "starts": starts, "done": done,
        "output": output, "cache": cache_rows(cache_path),
    }


def run_current(source: Path, directory: Path, options: TranslationOptions, *, change=None, glossary=None, seed=False, parallel=False):
    directory.mkdir()
    translator, statuses, starts, done = Prefix(change), [], [], []
    translator.parallel = parallel
    cache_path = directory / "cache.sqlite"
    destination = directory / "out.epub"
    factories = {"translator_factory": lambda: translator, "cache_factory": lambda: TranslationCache(cache_path)} if parallel else {}
    with TranslationCache(cache_path) as cache:
        if seed:
            translate_epub(source, directory / "seed.epub", translator, cache,
                           TranslationOptions(language_pair=PAIR, chapter_selection=ChapterSelection.parse("1")))
            translator.calls.clear()
        report = translate_epub(
            source, destination, translator, cache, options, glossary=glossary,
            on_chapter_start=lambda *item: starts.append(item), on_chapter_done=lambda index, total, name, stats: done.append(
                (index, total, name, stats.translated, stats.from_cache, stats.missing, list(stats.errors))),
            on_text_processed=statuses.append, **factories)
    output = zip_members(destination.read_bytes()) if report.output_written else None
    return observed(report, translator, statuses, starts, done, output, cache_path)


def run_engine(source: Path, directory: Path, options: TranslationOptions, formats, *, change=None, glossary=None, seed=False, parallel=False):
    directory.mkdir()
    translator, statuses, starts, done = Prefix(change), [], [], []
    translator.parallel = parallel
    cache_path = directory / "cache.sqlite"
    env = FormatEnv(directory / "env")
    publication = env.publication()
    factories = {"translator_factory": lambda: translator, "cache_factory": lambda: TranslationCache(cache_path)} if parallel else {}
    destination = directory.parent / "current" / "out.epub"
    with TranslationCache(cache_path) as cache:
        if seed:
            translate_epub(source, directory / "seed.epub", translator, cache,
                           TranslationOptions(language_pair=PAIR, chapter_selection=ChapterSelection.parse("1")))
            translator.calls.clear()
        report, result = translate_book(
            source, destination, translator, cache, options, formats=formats, source=LocalFormatSource(source), publication=publication,
            project_id=PROJECT, glossary=glossary, on_chapter_start=lambda *item: starts.append(item),
            on_chapter_done=lambda index, total, name, stats: done.append(
                (index, total, name, stats.translated, stats.from_cache, stats.missing, list(stats.errors))),
            on_text_processed=statuses.append, **factories)
    assert result.failure is None, result.failure
    output = zip_members(env.read(publication)) if result.output_written else None
    return observed(report, translator, statuses, starts, done, output, cache_path)


PARITY = {
    "plain": dict(options={}),
    "metadata-navigation-alt": dict(options=dict(translate_metadata=True, translate_alt_text=True)),
    "alt-only": dict(options=dict(translate_alt_text=True)),
    "chapter-by-index": dict(options=dict(chapter_selection=ChapterSelection.parse("2"))),
    "chapter-by-name": dict(options=dict(chapter_selection=ChapterSelection.parse("one.xhtml"))),
    "chapter-by-name-pattern": dict(options=dict(chapter_selection=ChapterSelection.parse("text/one*"))),
    "chapter-by-title": dict(options=dict(chapter_selection=ChapterSelection.parse("Second"))),
    "selection-with-metadata": dict(options=dict(translate_metadata=True, chapter_selection=ChapterSelection.parse("1"))),
    "small-chunks": dict(options=dict(chunk_limit=20)),
    "cache-only-empty": dict(options=dict(cache_only=True)),
    "cache-only-seeded": dict(options=dict(cache_only=True), seed=True),
    "cache-only-metadata": dict(options=dict(cache_only=True, translate_metadata=True, translate_alt_text=True), seed=True),
    "require-full-cache-blocked": dict(options=dict(cache_only=True, require_full_cache=True)),
    "second-run": dict(options={}, seed=True),
    "memory": dict(options=dict(translation_memory=TranslationMemoryOptions(apply_threshold=0.7, suggest_threshold=0.4)), seed=True),
    "failing-text": dict(options={}, change=failing_on("Second")),
    "failing-alt": dict(options=dict(translate_alt_text=True), change=failing_on("A picture")),
    "failing-title": dict(options=dict(translate_metadata=True), change=failing_on("The Title")),
    "broken-placeholders": dict(options={}, change=lambda text: text.replace("__AYVU_TAG_0__", "")),
    "unchanged-translation": dict(options=dict(translate_metadata=True, translate_alt_text=True), change=lambda text: text),
    "markup-like-translation": dict(options={}, change=lambda text: f"{text} <b>x</b> &amp; a < b ]]> <!-- c -->"),
    "newlines": dict(options={}, change=lambda text: f"a\r\nc\rd {text}"),
    "workers": dict(options=dict(workers=3), parallel=True),
    "workers-metadata-alt": dict(options=dict(workers=2, translate_metadata=True, translate_alt_text=True), parallel=True),
    "workers-failing-text": dict(options=dict(workers=2), parallel=True, change=failing_on("Second")),
}


def compare(tmp_path, formats, source, options, **scenario):
    current = run_current(source, tmp_path / "current", options, **scenario)
    engine = run_engine(source, tmp_path / "engine", options, formats, **scenario)
    assert engine == current
    return current


def engine_only(tmp_path, formats, data: bytes, *, change=None, **options):
    source = tmp_path / "book.epub"
    source.write_bytes(data)
    translator = Prefix(change)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(
            source, tmp_path / "out.epub", translator, cache, TranslationOptions(language_pair=PAIR, **options), formats=formats,
            source=LocalFormatSource(source), publication=publication, project_id=PROJECT)
    written = dict((item[0], item[3]) for item in zip_members(env.read(publication))) if result.output_written else None
    return report, result, translator, written
