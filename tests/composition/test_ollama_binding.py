"""The Ollama preflight and binding in the rich runtime: discovery first, then bounded workers (#376).

Real project, artifact, provenance and v2 cache stores and a fake Ollama on a real loopback socket.
"""
from __future__ import annotations

from zipfile import ZipFile

import pytest

from ayvu.composition.builders import translate_project as composition
from ayvu.composition.builders.ollama import OllamaPreflight, OllamaSettings, ollama_preflight, ollama_url
from ayvu.jobs.model import JobState
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.translation import PreflightError
from ayvu.translation.preflight import check_book_resources
from ayvu.translation.project_contracts import TranslateProjectCommand
from tests.support.cli_translate_project_support import _book
from tests.support.ollama_fakes import DIGEST_A, MODEL, fake_ollama  # noqa: F401

PAIR = LanguagePair(source="en", target="pt")


def prepare(tmp_path, url, *, workers=1, settings=OllamaSettings(), model=MODEL, **changes):
    arguments = dict(book_checks=check_book_resources, epub_path=tmp_path / "book.epub",
                     cache_path=tmp_path / "cache.sqlite", glossary_path=None, url=url, timeout=5.0, retries=0,
                     language_pair=PAIR, network_policy=None, cache_check=lambda _: None, model=model,
                     workers=workers, settings=settings)
    return ollama_preflight(**{**arguments, **changes})


def translate(tmp_path, prepared: OllamaPreflight, workers: int):
    source = tmp_path / "book.epub"
    projects = ProjectJsonRepository(tmp_path / "processing")
    project = projects.resolve(build_project_request(source, source="en", target="pt")).project
    paths = projects.resolve_database_paths(project.project_id)
    options = TranslationOptions(language_pair=PAIR, workers=workers)
    runtime = composition.RichTranslationRuntime(
        source, options, lambda: composition.LegacyResources(None, prepared.glossary, prepared.translator_factory),
        tmp_path / "cache.sqlite", MODEL, paths.database, paths.backups, projects.resolve_artifact_root(project.project_id),
        project.project_id, binding=prepared.binding)
    destination = tmp_path / "out.epub"
    use_case = composition.open_translate_project(tmp_path / "processing", project.project_id, source, destination,
                                                  overwrite=False, runtime=runtime)
    return use_case.execute(TranslateProjectCommand(
        project.project_id, project.source_sha256, options, OutputPlan.for_translation(source, destination, PAIR),
        execution_revision=2, producer=runtime.producer))


def test_two_workers_translate_when_the_configured_parallelism_allows_them(tmp_path, fake_ollama):
    _book(tmp_path / "book.epub")
    prepared = prepare(tmp_path, fake_ollama.url, workers=2, settings=OllamaSettings(parallelism=2))
    assert prepared.binding.producer.model == f"{MODEL}@{DIGEST_A}"
    result = translate(tmp_path, prepared, workers=2)
    assert result.state is JobState.VALIDATED
    assert "PT[Hello reader.]" in ZipFile(tmp_path / "out.epub").read("OEBPS/c1.xhtml").decode("utf-8")
    assert fake_ollama.chat_count() >= 3


def test_more_workers_than_the_parallelism_are_refused_before_discovery(tmp_path, fake_ollama):
    _book(tmp_path / "book.epub")
    with pytest.raises(PreflightError, match="at most 1 worker"):
        prepare(tmp_path, fake_ollama.url, workers=2)
    assert fake_ollama.requests == []


def test_the_book_and_glossary_checks_run_before_discovery(tmp_path, fake_ollama):
    with pytest.raises(PreflightError, match="EPUB"):
        prepare(tmp_path, fake_ollama.url)
    assert fake_ollama.requests == []


def test_each_translator_has_one_identity_source_for_admission():
    from ayvu.composition.builders.rich_provider import IdentitySource, discovers_identity, identity_source
    assert identity_source(" LibreTranslate ") == IdentitySource.DECLARED_LABEL
    assert identity_source("ollama") == IdentitySource.DISCOVERED_MODEL and discovers_identity("Ollama")
    assert identity_source("deepl") is None and not discovers_identity("libretranslate")


def test_the_libretranslate_default_url_means_ollamas_own():
    assert ollama_url("http://localhost:5000") == "http://localhost:11434"
    assert ollama_url("http://127.0.0.1:11500") == "http://127.0.0.1:11500"
