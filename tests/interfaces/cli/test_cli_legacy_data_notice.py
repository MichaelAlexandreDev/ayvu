"""REF-18 step 5 (path B): before new work, the CLI says that the old cache is kept and not reused."""
from __future__ import annotations

import pytest

from ayvu.interfaces.cli import app as cli
from tests.knowledge.cache.test_cache_legacy_census import state, v1_cache
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs

NOTICE = "Old translations:"


def flat(result):
    return " ".join(text(result).split())


def old_cache(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    return v1_cache(folder / "cache.sqlite")


def translate(tmp_path, monkeypatch, cache, *more, books=1):
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    sources = [str(_book(tmp_path / f"book{index}.epub")) for index in range(books)]
    output = [] if books > 1 else ["--output", str(tmp_path / "out.epub")]
    result = runner.invoke(cli.app, ["--mode", "developer", "translate", *sources, "--source", "en", "--target", "pt",
                                     "--url", "http://localhost:5000", "--retries", "0", "--cache", str(cache),
                                     *output, *more])
    return result, session


@pytest.mark.parametrize("more", [(), ("--dry-run",), ("--cache-only",)], ids=["translate", "dry-run", "cache-only"])
def test_an_old_cache_is_named_before_any_work_and_left_unchanged(tmp_path, monkeypatch, more):
    config(tmp_path)
    cache = old_cache(tmp_path)
    before = state(cache.parent)
    result, session = translate(tmp_path, monkeypatch, cache, *more)
    output = flat(result)
    assert result.exit_code == 0, output
    assert (f"{NOTICE} Ayvu's previous runtime left 2 texts for en→pt in {cache}. They are kept unchanged and are "
            "not reused: the translation starts again and is stored in "
            f"{cache.parent / 'cache.sqlite.v2.sqlite'}.") in output
    assert output.index("Translation plan") < output.index(NOTICE) < output.index("Translation report")
    after = state(cache.parent)
    assert after.pop("cache.sqlite") == before["cache.sqlite"]
    assert set(after) <= {"cache.sqlite.v2.sqlite", "cache.sqlite.v2.sqlite.lock"}
    if more == ():
        assert any(method == "POST" for method, _, _ in session.requests)  # The old texts are translated again.


def test_without_an_old_cache_nothing_is_said(tmp_path, monkeypatch):
    config(tmp_path)
    result, _ = translate(tmp_path, monkeypatch, tmp_path / "fresh" / "cache.sqlite")
    assert result.exit_code == 0, flat(result)
    assert NOTICE not in flat(result)


def test_an_old_cache_for_other_languages_is_not_mentioned(tmp_path, monkeypatch):
    config(tmp_path)
    folder = tmp_path / "old"
    folder.mkdir()
    cache = v1_cache(folder / "cache.sqlite", rows=(("en", "es", "Hi"), ("fr", "pt", "Bonjour")))
    result, _ = translate(tmp_path, monkeypatch, cache)
    assert result.exit_code == 0, flat(result)
    assert NOTICE not in flat(result)


def test_an_unreadable_old_cache_is_reported_and_the_translation_goes_on(tmp_path, monkeypatch):
    config(tmp_path)
    cache = old_cache(tmp_path)
    (cache.parent / "cache.sqlite-journal").write_bytes(b"interrupted")
    result, _ = translate(tmp_path, monkeypatch, cache)
    output = flat(result)
    assert result.exit_code == 0, output
    assert f"{NOTICE} {cache} may hold translations from Ayvu's previous runtime, but it could not be read." in output
    assert (cache.parent / "cache.sqlite-journal").read_bytes() == b"interrupted"


def test_a_batch_says_it_once_before_its_books(tmp_path, monkeypatch):
    config(tmp_path)
    cache = old_cache(tmp_path)
    result, _ = translate(tmp_path, monkeypatch, cache, books=2)
    output = flat(result)
    assert result.exit_code == 0, output
    assert output.count(NOTICE) == 1
    assert output.index(NOTICE) < output.index("Batch item 1/2")


def test_without_a_label_the_refusal_comes_first_and_alone(tmp_path, monkeypatch):
    config(tmp_path, label=None)
    result, session = translate(tmp_path, monkeypatch, old_cache(tmp_path))
    assert result.exit_code == 1 and NOTICE not in flat(result) and session.requests == []


def test_the_continuation_of_a_rich_job_does_not_repeat_it(tmp_path, monkeypatch):
    config(tmp_path)
    cache = old_cache(tmp_path)
    session = Transport(failing="Goodbye reader.")
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    first = runner.invoke(cli.app, ["--mode", "developer", "translate", str(_book(tmp_path / "book.epub")), "--source",
                                    "en", "--target", "pt", "--url", "http://localhost:5000", "--retries", "0",
                                    "--cache", str(cache), "--output", str(tmp_path / "out.epub"), "--fail-fast"])
    assert NOTICE in flat(first)
    parent = _jobs(tmp_path)[-1]
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: Transport())
    resumed = runner.invoke(cli.app, ["--mode", "developer", "resume", str(tmp_path / "book.epub")])
    assert resumed.exit_code == 0, flat(resumed)
    assert any(job.continuation_of == parent.job_id for job in _jobs(tmp_path))
    assert NOTICE not in flat(resumed)


@pytest.mark.parametrize("label", [None, "local-model-2026"], ids=["without-label", "with-label"])
def test_a_v1_job_is_shown_as_previous_runtime_and_refused(tmp_path, monkeypatch, label):
    from tests.interfaces.cli.test_cli_resume_runtime import interrupted, resume
    parent = interrupted(tmp_path, monkeypatch, None)
    assert parent.request.command_version == 1
    config(tmp_path, label=label)
    result, _ = resume(tmp_path, monkeypatch)
    output = flat(result)
    assert result.exit_code == 1, output
    assert "Previous runtime job: book.epub" in output and "Runtime │ previous (v1)" in output
    assert ("This job started on Ayvu's previous runtime, which this version no longer runs. Finish it with the "
            "frozen release v1-runtime-final, or translate the book again. Its files were left unchanged.") in output
    assert NOTICE not in output  # Refusing old work is not new work.


def test_resuming_a_rich_job_says_nothing_about_the_old_runtime(tmp_path, monkeypatch):
    from tests.interfaces.cli.test_cli_resume_runtime import interrupted, resume
    interrupted(tmp_path, monkeypatch, "local-model-2026")
    result, _ = resume(tmp_path, monkeypatch)
    assert result.exit_code == 0, flat(result)
    assert "Old job:" not in flat(result) and "previous (v1)" not in flat(result)


def test_guided_translation_names_the_default_old_cache(tmp_path, monkeypatch):
    from tests.interfaces.cli.test_cli_rich_surfaces import guided
    config(tmp_path)
    (tmp_path / ".cache").mkdir()
    cache = v1_cache(tmp_path / ".cache" / "traducoes.sqlite")
    result, _ = guided(tmp_path, monkeypatch)
    output = flat(result)
    assert result.exit_code == 0, output
    assert f"{NOTICE} Ayvu's previous runtime left 2 texts for en→pt in .cache/traducoes.sqlite." in output
    assert ".cache/traducoes.sqlite.v2.sqlite" in output
    assert cache.is_file()


def test_an_old_checkpoint_is_refused_and_its_old_cache_left_alone(tmp_path, monkeypatch):
    from tests.interfaces.cli.test_cli_recovery import _old_file
    source = _book(tmp_path / "book.epub")
    v1_cache(tmp_path / "cache.sqlite")  # The checkpoint's own v1 cache.
    _old_file(tmp_path, source)
    from tests.support.model_label_support import snapshot
    before = snapshot(tmp_path)
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: Transport())
    result = runner.invoke(cli.app, ["--mode", "developer", "resume"])
    output = flat(result)
    assert result.exit_code == 1, output
    assert "no longer runs" in output and NOTICE not in output
    assert snapshot(tmp_path) == before
