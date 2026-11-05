"""The translation engine against the current flow, which is the oracle until #145 retires it.

Each parity scenario runs both and compares everything a user can observe: the bytes, the calls
to the provider and their order, the cache, the report and the progress. Review export now belongs
to the shared review application use case, which consumes the engine's stable documents and units.
"""
from __future__ import annotations

import io
import sqlite3
import threading
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from ayvu.cache import TranslationCache
from ayvu.domain import ChapterSelection, ChapterSelectionError, LanguagePair, TranslationMemoryOptions, TranslationOptions
from ayvu.epub_format import EPUB_REGISTRATION, epub_format_adapter
from ayvu.epub_io import translate_epub
from ayvu.format_local import LocalFormatSource
from ayvu.format_service import FormatRegistry, FormatService
from ayvu.glossary import load_glossary
from ayvu.format_adapter import MaterializationResult
from ayvu.job_events import JobFailure, JobFailureFamily
from ayvu.legacy_translation import translate_book
from epub_fakes import NAV, OPF, TWO, XHTML, epub_bytes, members, zip_bytes
from format_contract import PROJECT, FormatEnv

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


@pytest.mark.parametrize("name", PARITY)
def test_the_engine_matches_the_current_flow(name, tmp_path, formats):
    scenario = dict(PARITY[name])
    options = TranslationOptions(language_pair=PAIR, **scenario.pop("options"))
    source = book(tmp_path)
    current = run_current(source, tmp_path / "current", options, **scenario)
    engine = run_engine(source, tmp_path / "engine", options, formats, **scenario)
    for part in current:
        assert engine[part] == current[part], part


def test_the_engine_matches_the_current_flow_with_a_glossary(tmp_path, formats):
    path = tmp_path / "glossary.json"
    path.write_text('{"Hello": "Ola", "chapter": "capitulo"}', encoding="utf-8")
    options = TranslationOptions(language_pair=PAIR, translate_metadata=True)
    source = book(tmp_path)
    current = run_current(source, tmp_path / "current", options, glossary=load_glossary(path))
    engine = run_engine(source, tmp_path / "engine", options, formats, glossary=load_glossary(path))
    assert engine == current


def compare(tmp_path, formats, source, options, **scenario):
    current = run_current(source, tmp_path / "current", options, **scenario)
    engine = run_engine(source, tmp_path / "engine", options, formats, **scenario)
    assert engine == current
    return current


def test_the_engine_matches_the_current_flow_with_a_glossary_in_workers(tmp_path, formats):
    path = tmp_path / "glossary.json"
    path.write_text('{"Hello": "Ola", "chapter": "capitulo"}', encoding="utf-8")
    seen = compare(tmp_path, formats, book(tmp_path), TranslationOptions(language_pair=PAIR, workers=2), parallel=True, glossary=load_glossary(path))
    assert seen["glossary"][0]


def test_the_engine_reports_a_required_glossary_term_that_was_never_used(tmp_path, formats):
    path = tmp_path / "glossary.json"
    path.write_text('{"Absent term": {"rule": "translate", "translation": "Termo", "required": true}}', encoding="utf-8")
    seen = compare(tmp_path, formats, book(tmp_path), TranslationOptions(language_pair=PAIR), glossary=load_glossary(path))
    assert seen["glossary"][1]


def test_the_engine_matches_the_current_flow_when_memory_applies_a_similar_text(tmp_path, formats):
    source = book(tmp_path, chapter=XHTML.format("<p>The quick brown fox jumps over the lazy dog and runs</p>").encode())
    with ZipFile(source) as archive:
        items = {info.filename: archive.read(info) for info in archive.infolist()}
    items["OEBPS/text/two words.xhtml"] = XHTML.format("<p>The quick brown fox jumps over the lazy dog and ran</p>").replace("Doc", "Second doc").encode()
    source.write_bytes(epub_bytes(replace=items))
    options = TranslationOptions(language_pair=PAIR, translation_memory=TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.5))
    seen = compare(tmp_path, formats, source, options, seed=True)
    assert seen["report"]["texts_from_memory"] == 1 and "memory" in seen["statuses"]


def test_the_engine_matches_the_current_flow_on_a_missing_text_holding_a_carriage_return(tmp_path, formats):
    source = book(tmp_path, chapter=XHTML.format("<p>Line one&#13;line two</p>").encode())
    compare(tmp_path, formats, source, TranslationOptions(language_pair=PAIR, cache_only=True))


def test_fail_fast_with_workers_stops_and_publishes_nothing(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ConnectionError):
        translate_book(source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2, fail_fast=True),
                       formats=formats, source=LocalFormatSource(source), publication=publication, project_id=PROJECT,
                       translator_factory=lambda: Prefix(failing_on("Second")), cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert not env.is_ready(publication)


def test_a_book_the_adapter_cannot_rebuild_reports_no_output(tmp_path, formats, monkeypatch):
    source = book(tmp_path)
    failure = JobFailure(JobFailureFamily.INTEGRITY, "source_changed")
    monkeypatch.setattr(formats, "reconstruct", lambda *_args, **_kwargs: MaterializationResult(failure=failure))
    env = FormatEnv(tmp_path / "env")
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR), formats=formats,
                                        source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT)
    assert result.failure == failure and result.artifact is None and not result.output_written and not report.output_written


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


@pytest.mark.parametrize("build,code", [
    (lambda: epub_bytes(add=[("../evil", b"x", ZIP_STORED)]), "unsafe_member_name"),
    (lambda: epub_bytes(add=[("OEBPS\\x.txt", b"x", ZIP_DEFLATED)]), "unsafe_member_name"),
    (lambda: zip_bytes([*members(), members()[3]]), "duplicate_member"),
    (lambda: zip_bytes([*members(), (members()[3][0].upper(), b"x", ZIP_DEFLATED)]), "duplicate_member"),
    (lambda: epub_bytes(replace={"OEBPS/text/one.xhtml": b'<!DOCTYPE html [<!ENTITY e "E">]><html><body><p>A &e; B</p></body></html>'}), "entity_declaration"),
    (lambda: epub_bytes(add=[("META-INF/encryption.xml", b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/><enc:CipherData><enc:CipherReference URI="OEBPS/text/one.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>', ZIP_DEFLATED)]), "encrypted_content"),
], ids=["traversal", "backslash", "duplicate", "case-duplicate", "entity", "encrypted"])
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_a_refused_book_calls_no_provider_and_publishes_nothing(tmp_path, formats, build, code):
    report, result, translator, written = engine_only(tmp_path, formats, build())
    assert result.failure.code == code and not translator.calls and written is None and not report.output_written


@pytest.mark.parametrize("data", [
    epub_bytes(drop=("mimetype",)),
    zip_bytes([*members()[1:], members()[0]]),
    zip_bytes([("mimetype", b"application/epub+zip", ZIP_DEFLATED), *members()[1:]]),
    epub_bytes(replace={"mimetype": b"text/plain"}),
], ids=["missing", "not-first", "compressed", "other-content"])
def test_a_defective_mimetype_is_translated_and_comes_out_correct(tmp_path, formats, data):
    _, result, translator, written = engine_only(tmp_path, formats, data)
    assert result.failure is None and translator.calls
    assert next(iter(written)) == "mimetype" and written["mimetype"] == b"application/epub+zip"
    assert b"PT:Hello" in written["OEBPS/text/one.xhtml"]


def test_a_remote_manifest_item_no_longer_stops_a_book(tmp_path, formats):
    _, result, translator, written = engine_only(tmp_path, formats, epub_bytes())
    assert result.failure is None and translator.calls and b"PT:Hello" in written["OEBPS/text/one.xhtml"]


def test_a_processing_instruction_in_the_metadata_no_longer_stops_a_book(tmp_path, formats):
    opf = OPF.replace(b"<metadata>", b"<metadata><?keep this?>", 1)
    _, result, _, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True)
    assert result.failure is None and b"<?keep this?>" in written[OPF_MEMBER] and b"PT:The Title" in written[OPF_MEMBER]


def test_a_document_with_nothing_to_translate_keeps_its_bytes(tmp_path, formats):
    empty = b"<html><body><img src='../images/a.png'/></body></html>"
    _, result, _, written = engine_only(tmp_path, formats, epub_bytes(replace={"OEBPS/text/one.xhtml": empty}))
    assert result.failure is None and written["OEBPS/text/one.xhtml"] == empty


@pytest.mark.parametrize("title", [b"The<!-- c --> Title", b"<!-- c -->The Title", b"The <span>Title</span>", b"The<?keep this?> Title"])
def test_a_title_holding_other_nodes_is_reported_and_keeps_the_package_bytes(tmp_path, formats, title):
    opf = OPF.replace(b"The Title", title, 1)
    report, result, translator, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True)
    assert written[OPF_MEMBER] == opf and result.failure is None
    assert report.errors == ["metadata title: the title holds a comment, element or entity, so it stays untranslated"]
    assert not [call for call in translator.calls if "Title" in call]


def test_a_title_holding_other_nodes_stops_a_fail_fast_run(tmp_path, formats):
    opf = OPF.replace(b"The Title", b"The<!-- c --> Title", 1)
    with pytest.raises(ValueError, match="stays untranslated"):
        engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}), translate_metadata=True, fail_fast=True)


def test_a_title_holding_other_nodes_is_ignored_without_translate_metadata(tmp_path, formats):
    opf = OPF.replace(b"The Title", b"The<!-- c --> Title", 1)
    report, _, _, written = engine_only(tmp_path, formats, epub_bytes(replace={OPF_MEMBER: opf}))
    assert report.errors == [] and written[OPF_MEMBER] == opf


def test_a_title_translation_xml_cannot_hold_leaves_the_package_bytes_and_is_reported(tmp_path, formats):
    report, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(), translate_metadata=True, change=lambda text: f"a\x0bb {text}" if "The Title" in text else f"PT:{text}")
    assert written[OPF_MEMBER] == OPF and result.failure is None
    assert [error for error in report.errors if error.startswith("metadata title: ")]


def test_invalid_characters_in_a_translation_are_dropped_without_losing_text(tmp_path, formats):
    """Differs from the current flow on purpose: there, the same text also loses its ``&`` and ``<tags>``."""
    _, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(replace={"OEBPS/text/one.xhtml": HARD_CHAPTER}), change=lambda text: f"a\x0bb\x00c {text}")
    chapter = written["OEBPS/text/one.xhtml"]
    assert result.failure is None and b"\x0b" not in chapter and b"\x00" not in chapter
    assert b"abc Text &amp; more &lt;tags&gt;" in chapter


def test_an_alt_text_with_invalid_characters_still_gives_a_valid_document(tmp_path, formats):
    _, result, _, written = engine_only(
        tmp_path, formats, epub_bytes(), translate_alt_text=True, change=lambda text: f"a\x0bb {text}")
    assert result.failure is None and b"\x0b" not in written["OEBPS/text/one.xhtml"]


def test_a_selection_that_matches_nothing_raises_before_any_call(tmp_path, formats):
    source = book(tmp_path)
    translator = Prefix()
    env = FormatEnv(tmp_path / "env")
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ChapterSelectionError, match="no chapters matched selection: 99"):
        translate_book(source, tmp_path / "out.epub", translator, cache,
                       TranslationOptions(language_pair=PAIR, chapter_selection=ChapterSelection.parse("99")), formats=formats,
                       source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT)
    assert not translator.calls


def test_fail_fast_stops_at_the_first_failed_text_and_publishes_nothing(tmp_path, formats):
    source = book(tmp_path)
    translator = Prefix(failing_on("Text"))
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ConnectionError):
        translate_book(source, tmp_path / "out.epub", translator, cache, TranslationOptions(language_pair=PAIR, fail_fast=True),
                       formats=formats, source=LocalFormatSource(source), publication=publication, project_id=PROJECT)
    assert not env.is_ready(publication)


def test_workers_each_get_their_own_executor_and_results_stay_in_book_order(tmp_path, formats):
    source = book(tmp_path)
    created: list[Prefix] = []

    def new_translator() -> Prefix:
        created.append(Prefix())
        return created[-1]

    env = FormatEnv(tmp_path / "env")
    starts: list[int] = []
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        _, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2), formats=formats,
            source=LocalFormatSource(source), publication=env.publication(), project_id=PROJECT,
            on_chapter_start=lambda index, *_: starts.append(index), translator_factory=new_translator,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert result.failure is None and starts == [1, 2] and len(created) == 2


def test_a_worker_that_cannot_start_leaves_its_chapter_alone_and_is_reported(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()
    created = []

    def new_translator() -> Prefix:
        created.append(1)
        if len(created) == 1:
            raise OSError("no session")
        return Prefix()

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=1 + 1), formats=formats,
            source=LocalFormatSource(source), publication=publication, project_id=PROJECT, translator_factory=new_translator,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert result.failure is None and len(report.errors) == 1 and "no session" in report.errors[0]


def test_no_chapter_finished_means_no_output(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    publication = env.publication()

    def broken() -> Prefix:
        raise OSError("no session")

    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report, result = translate_book(
            source, tmp_path / "out.epub", Prefix(), cache, TranslationOptions(language_pair=PAIR, workers=2), formats=formats,
            source=LocalFormatSource(source), publication=publication, project_id=PROJECT, translator_factory=broken,
            cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INPUT, "nothing_to_materialize")
    assert not report.output_written and not env.is_ready(publication)


def test_memory_and_workers_are_refused_as_in_the_current_flow(tmp_path, formats):
    source = book(tmp_path)
    env = FormatEnv(tmp_path / "env")
    options = TranslationOptions(language_pair=PAIR, workers=2, translation_memory=TranslationMemoryOptions())
    with TranslationCache(tmp_path / "cache.sqlite") as cache, pytest.raises(ValueError, match="translation memory is not supported"):
        translate_book(source, tmp_path / "out.epub", Prefix(), cache, options, formats=formats, source=LocalFormatSource(source),
                       publication=env.publication(), project_id=PROJECT, translator_factory=Prefix,
                       cache_factory=lambda: TranslationCache(tmp_path / "cache.sqlite"))


def test_results_omit_book_text_from_their_repr(tmp_path, formats):
    _, result, _, _ = engine_only(tmp_path, formats, epub_bytes(), translate_metadata=True)
    shown = repr(result) + repr(result.stats)
    assert "Title" not in shown and "Hello" not in shown
