"""The translation engine against the current flow, which is the oracle until #145 retires it.

Each parity scenario runs both and compares everything a user can observe: the bytes, the calls
to the provider and their order, the cache, the report and the progress. Review export now belongs
to the shared review application use case, which consumes the engine's stable documents and units.
"""
from __future__ import annotations

from zipfile import ZipFile

import pytest

from ayvu.knowledge.glossary.rules import load_glossary
from tests.support.epub_fakes import XHTML, epub_bytes
from ayvu.kernel import TranslationMemoryOptions, TranslationOptions

from tests.v1.translation_engine_support import PAIR, PARITY
from tests.v1.translation_engine_support import book, compare, formats
from tests.v1.translation_engine_support import run_current, run_engine


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
