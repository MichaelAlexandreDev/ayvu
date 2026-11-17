"""The rich runtime translates a small EPUB through the LLM core with a fake model (#375).

It runs the same door the CLI uses (``open_translate_project`` with a ``RichTranslationRuntime``), with
real project, artifact, provenance and v2 cache stores; only the model is fake.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from zipfile import ZIP_STORED, ZipFile

from ayvu.composition.builders import translate_project as composition
from ayvu.composition.builders.llm import LlmBinding
from ayvu.jobs.model import JobState
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.knowledge.glossary import Glossary
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.providers.llm import PROMPT_V1, LlmSettings
from ayvu.translation.project_contracts import TranslateProjectCommand
from tests.support.cli_translate_project_support import _book
from tests.support.llm_fakes import LLM_PRODUCER, FakeModel, fake_snapshot

PAIR = LanguagePair(source="en", target="pt")
CHAPTER_TWO = '<p>Goodbye <em>dear</em> reader, see <a href="c1.xhtml#top">the start</a>.</p>'


def translate(tmp_path, source, model, output, *, settings=LlmSettings(), glossary=None):
    projects = ProjectJsonRepository(tmp_path / "processing")
    project = projects.resolve(build_project_request(source, source="en", target="pt")).project
    paths = projects.resolve_database_paths(project.project_id)
    options = TranslationOptions(language_pair=PAIR)
    binding = LlmBinding(lambda: model, LLM_PRODUCER, fake_snapshot, clock=composition._utc_now, settings=settings)
    runtime = composition.RichTranslationRuntime(
        source, options, lambda: composition.LegacyResources(None, glossary), tmp_path / "cache.sqlite", "unused",
        paths.database, paths.backups, projects.resolve_artifact_root(project.project_id), project.project_id,
        binding=binding)
    destination = tmp_path / output
    use_case = composition.open_translate_project(tmp_path / "processing", project.project_id, source, destination,
                                                  overwrite=False, runtime=runtime)
    command = TranslateProjectCommand(project.project_id, project.source_sha256, options,
                                      OutputPlan.for_translation(source, destination, PAIR), execution_revision=2,
                                      producer=runtime.producer, additional_output_roles=("raw_results",))
    return use_case.execute(command), destination


def v2_rows(tmp_path) -> int:
    """Read-only count of the test's own temporary v2 cache."""
    with closing(sqlite3.connect(composition.v2_cache_path(tmp_path / "cache.sqlite"))) as connection:
        return connection.execute("SELECT count(*) FROM cache_entries_v2").fetchone()[0]


def tags(markup: bytes) -> list[str]:
    return re.findall(r"<[^>]+>", markup.decode("utf-8"))


def test_an_epub_is_translated_through_the_core_with_its_structure_preserved(tmp_path):
    source = _book(tmp_path / "book.epub", chapter_two=CHAPTER_TWO)
    before = source.read_bytes()
    model = FakeModel()
    result, output = translate(tmp_path, source, model, "out.epub", glossary=Glossary({"reader": "leitor"}))

    assert result.state is JobState.VALIDATED
    assert source.read_bytes() == before
    with ZipFile(source) as original, ZipFile(output) as translated:
        assert translated.namelist() == original.namelist()
        assert translated.namelist()[0] == "mimetype"
        assert translated.getinfo("mimetype").compress_type == ZIP_STORED
        for name in ("OEBPS/c1.xhtml", "OEBPS/c2.xhtml", "OEBPS/nav.xhtml"):
            assert tags(translated.read(name)) == tags(original.read(name)), name
        chapter_one = translated.read("OEBPS/c1.xhtml").decode("utf-8")
        chapter_two = translated.read("OEBPS/c2.xhtml").decode("utf-8")
    assert "<h1>PT[Hello title]</h1>" in chapter_one
    # The model never applied the glossary: the pipeline did, after translation, as for LibreTranslate.
    assert "<p>PT[Hello leitor.]</p>" in chapter_one
    assert '<a href="c1.xhtml#top">' in chapter_two and "<em>" in chapter_two
    assert model.requests and all(request.system == PROMPT_V1.system for request in model.requests)
    assert json.loads(model.requests[0].user)["glossary"] == [
        {"rule": "translate", "term": "reader", "translation": "leitor"}]
    assert model.closed >= 1


def test_the_v2_cache_serves_the_same_prompt_and_never_another_revision(tmp_path):
    source = _book(tmp_path / "book.epub")
    first = FakeModel()
    assert translate(tmp_path, source, first, "first.epub")[0].state is JobState.VALIDATED
    assert first.requests
    stored = v2_rows(tmp_path)
    assert stored > 0

    again = FakeModel()
    result, output = translate(tmp_path, source, again, "again.epub")
    assert result.state is JobState.VALIDATED and again.requests == [] and v2_rows(tmp_path) == stored
    assert "PT[Hello reader.]" in ZipFile(output).read("OEBPS/c1.xhtml").decode("utf-8")

    revised = FakeModel(translate=lambda text: f"V2[{text}]")
    result, output = translate(tmp_path, source, revised, "revised.epub",
                               settings=LlmSettings(prompt=replace(PROMPT_V1, revision=2)))
    assert result.state is JobState.VALIDATED and revised.requests and v2_rows(tmp_path) == 2 * stored
    assert "V2[Hello reader.]" in ZipFile(output).read("OEBPS/c1.xhtml").decode("utf-8")
