"""Translating with Ollama from the CLI, end to end, against a fake Ollama on a real loopback socket (#376).

Production composition, stores and HTTP stack all run; only the server is fake. HOME, XDG_CONFIG_HOME and
the working directory are isolated by the ``isolate`` fixture.
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
from zipfile import ZIP_STORED, ZipFile

import pytest

import ayvu.composition.builders.translate_project as composition
from ayvu.interfaces.cli import app as cli
from ayvu.jobs.model import JobState
from tests.support.cli_rich_runtime_support import isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.ollama_fakes import DIGEST_A, DIGEST_B, MODEL, fake_ollama  # noqa: F401

CHAPTER_TWO = '<p>Goodbye <em>dear</em> reader, see <a href="c1.xhtml#top">the start</a>.</p>'


def configure(tmp_path, models):
    path = tmp_path / "config" / "ayvu" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"books_dir": str(tmp_path / "books"), "translator_models": models}))


def translate(tmp_path, url, *more, source=None):
    source = source or tmp_path / "book.epub"
    return runner.invoke(cli.app, ["--mode", "developer", "translate", str(source), "--source", "en", "--target", "pt",
                                   "--translator", "ollama", "--url", url, "--retries", "0",
                                   "--cache", str(tmp_path / "cache.sqlite"), "--output", str(tmp_path / "out.epub"),
                                   *more])


def tags(markup: bytes) -> list[str]:
    return re.findall(r"<[^>]+>", markup.decode("utf-8"))


def test_a_book_is_translated_through_the_rich_runtime_with_its_structure_preserved(tmp_path, fake_ollama):
    configure(tmp_path, {"ollama": MODEL})
    source = _book(tmp_path / "book.epub", chapter_two=CHAPTER_TWO)
    original = source.read_bytes()
    result = translate(tmp_path, fake_ollama.url)
    assert result.exit_code == 0, text(result)
    assert source.read_bytes() == original
    with ZipFile(source) as before, ZipFile(tmp_path / "out.epub") as after:
        assert after.namelist() == before.namelist() and after.namelist()[0] == "mimetype"
        assert after.getinfo("mimetype").compress_type == ZIP_STORED
        for name in ("OEBPS/c1.xhtml", "OEBPS/c2.xhtml", "OEBPS/nav.xhtml"):
            assert tags(after.read(name)) == tags(before.read(name)), name
        assert "<p>PT[Hello reader.]</p>" in after.read("OEBPS/c1.xhtml").decode("utf-8")
        assert '<a href="c1.xhtml#top">' in after.read("OEBPS/c2.xhtml").decode("utf-8")
    assert fake_ollama.requests[0] == ("GET", "/api/tags")
    assert {path for _, path in fake_ollama.requests} == {"/api/tags", "/api/chat"}
    assert all(body["model"] == MODEL and body["stream"] is False for body in fake_ollama.chat_bodies)
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == "translate_project")
    assert job.state is JobState.VALIDATED and job.request.command_version == 2
    producers = {record.producer for record in records(tmp_path, job)}
    assert {(item.provider, item.model) for item in producers} == {("ollama", f"{MODEL}@{DIGEST_A}")}


def test_the_same_digest_is_served_from_the_cache_and_a_new_digest_is_not(tmp_path, fake_ollama):
    configure(tmp_path, {"ollama": MODEL})
    _book(tmp_path / "book.epub")
    assert translate(tmp_path, fake_ollama.url).exit_code == 0
    first = fake_ollama.chat_count()
    assert first > 0
    assert translate(tmp_path, fake_ollama.url, "--overwrite").exit_code == 0
    assert fake_ollama.chat_count() == first
    fake_ollama.models = [(MODEL, DIGEST_B)]  # `ollama pull` brought new weights under the same name
    result = translate(tmp_path, fake_ollama.url, "--overwrite")
    assert result.exit_code == 0, text(result)
    assert fake_ollama.chat_count() == 2 * first
    with sqlite3.connect(composition.v2_cache_path(tmp_path / "cache.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (2 * first,)


def test_a_missing_model_is_refused_before_any_job_or_chat(tmp_path, fake_ollama):
    configure(tmp_path, {"ollama": "llama3.2:3b"})
    _book(tmp_path / "book.epub")
    result = translate(tmp_path, fake_ollama.url)
    assert result.exit_code == 1
    assert "The Ollama model is not installed." in text(result) and "ollama pull llama3.2:3b" in text(result)
    assert fake_ollama.requests == [("GET", "/api/tags")]
    assert [job for job in _jobs(tmp_path) if job.request.command_type == "translate_project"] == []
    assert not (tmp_path / "out.epub").exists()


@pytest.mark.parametrize("more,models,expected", [
    (("--workers", "2"), {"ollama": MODEL}, "Ollama runs with at most 1 worker(s) here."),
    (("--cache-only",), {"ollama": MODEL}, "not available with Ollama yet"),
    ((), {}, "Ollama needs an installed model"),
    (("--target", "xx"), {"ollama": MODEL}, "The language pair is not declared for Ollama."),
])
def test_what_ollama_cannot_run_is_refused_before_any_request(tmp_path, fake_ollama, more, models, expected):
    configure(tmp_path, models)
    _book(tmp_path / "book.epub")
    result = translate(tmp_path, fake_ollama.url, *more)
    assert result.exit_code == 1 and expected in text(result), text(result)
    # The declared-label gate admits Ollama: every refusal here is Ollama's own.
    assert "declare your translator" not in text(result)
    assert fake_ollama.requests == []


def test_network_policy_none_refuses_ollama_before_any_socket(tmp_path, fake_ollama, monkeypatch):
    configure(tmp_path, {"ollama": MODEL})
    _book(tmp_path / "book.epub")

    def forbidden(*args, **kwargs):
        raise AssertionError("a socket was opened under --network-policy none")

    monkeypatch.setattr(socket, "socket", forbidden)
    result = translate(tmp_path, fake_ollama.url, "--network-policy", "none")
    assert result.exit_code == 1 and "strict_offline" in text(result), text(result)
    # The policy refusal itself, with its next step, not a later failure to reach the server.
    assert "O acesso HTTP ao tradutor foi bloqueado." in text(result) and "--network-policy loopback" in text(result)
    assert fake_ollama.requests == []


def test_test_translator_and_languages_work_for_ollama(tmp_path, fake_ollama):
    configure(tmp_path, {"ollama": MODEL})
    tested = runner.invoke(cli.app, ["test-translator", "--translator", "ollama", "--url", fake_ollama.url])
    assert tested.exit_code == 0, text(tested)
    assert "Translator OK:" in text(tested) and "PT[Hello world]" in text(tested)
    listed = runner.invoke(cli.app, ["languages", "--translator", "ollama", "--url", fake_ollama.url])
    assert listed.exit_code == 0, text(listed)
    assert "Ollama languages" in text(listed) and "Portuguese" in text(listed)
    missing = runner.invoke(cli.app, ["test-translator", "--translator", "ollama", "--url", fake_ollama.url,
                                      "--model", "missing:1b"])
    assert missing.exit_code == 1 and fake_ollama.chat_count() == 1
