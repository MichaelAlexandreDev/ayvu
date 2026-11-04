"""The real ``resume`` command and the guided offer, through the recovery use cases, watched from the jobs and the files they leave."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ayvu.cli import app
from ayvu.config import AyvuConfig, ConfigStore, TranslationProfile, default_config_path
from ayvu.job import JobState
from ayvu.resume import ResumeStateStore
from test_cli_translate_project import ENCRYPTED_DOCUMENT, Session, _args, _book, _jobs, _only_job
from test_job_recovery_concurrency import CHILD

runner = CliRunner()
RESUME = ["--mode", "developer", "resume"]
WRAPPER = "Não foi possível retomar a tradução detectada."


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.cli._processing_dir", lambda _config: tmp_path / "processing")


def _stop_on(text, error=KeyboardInterrupt):
    def stop(sent):
        if text in sent:
            raise error
    return stop


def _invoke(monkeypatch, arguments, session=None, stdin=None):
    session = session or Session()
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: session)
    return runner.invoke(app, arguments, input=stdin), session


def _interrupted(tmp_path, monkeypatch, *more, source=None):
    source = source or _book(tmp_path / "book.epub")
    result, _ = _invoke(monkeypatch, _args(tmp_path, source, *more), Session(on_post=_stop_on("Goodbye")))
    assert result.exit_code == 1, result.output
    return source


def _state_file(tmp_path) -> Path:
    return next((tmp_path / "processing").glob("*.ayvu-state.json"))


def _state(tmp_path) -> dict:
    return json.loads(_state_file(tmp_path).read_text())


def _chain(tmp_path):
    jobs = sorted(_jobs(tmp_path), key=lambda job: job.created_at)
    return [(job.state, None if job.terminal_summary is None else job.terminal_summary.reason_code) for job in jobs], jobs


def test_a_resume_is_a_linked_job_repeats_only_what_is_missing_and_finishes_the_file(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    interrupted = _only_job(tmp_path)
    assert interrupted.state is JobState.CANCELLED

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 0, result.output
    assert session.posts == ["Goodbye reader."] and "Resume checkpoint" in result.output
    states, jobs = _chain(tmp_path)
    assert states == [(JobState.CANCELLED, "cancelled"), (JobState.VALIDATED, "validated")]
    assert jobs[1].continuation_of == jobs[0].job_id
    assert _state(tmp_path)["status"] == "completed" and (tmp_path / "out.epub").exists()


def test_the_guided_offer_resumes_the_same_way(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)

    result, session = _invoke(monkeypatch, ["--mode", "common"], stdin="y\nn\n")

    assert result.exit_code == 0, result.output
    assert "Continue detected translation?" in result.output and session.posts == ["Goodbye reader."]
    assert _chain(tmp_path)[1][1].continuation_of == _chain(tmp_path)[1][0].job_id


def test_declining_the_offer_changes_nothing(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    before = _state_file(tmp_path).read_bytes()
    jobs_before = _jobs(tmp_path)

    result, session = _invoke(monkeypatch, ["--mode", "common"], stdin="n\n")

    assert "Processing files were left unchanged" in result.output and not session.posts
    assert _state_file(tmp_path).read_bytes() == before and _jobs(tmp_path) == jobs_before


def test_resuming_after_finishing_finds_nothing_and_adds_no_job(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    _invoke(monkeypatch, RESUME)
    count = len(_jobs(tmp_path))

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 1 and "No translation in progress" in result.output
    assert not session.posts and len(_jobs(tmp_path)) == count


def _live_run(tmp_path):
    source = _book(tmp_path / "book.epub")
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "processing"), *_args(tmp_path, source)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment, cwd=tmp_path)
    assert child.stdout.readline() == b"blocked\n"
    return child


def test_a_live_run_is_not_run_over_and_after_a_kill_the_resume_goes_through(tmp_path, monkeypatch):
    child = _live_run(tmp_path)
    try:
        before = _state_file(tmp_path).read_bytes()
        refused, session = _invoke(monkeypatch, RESUME)
        assert refused.exit_code == 1 and "em andamento" in refused.output and WRAPPER in " ".join(refused.output.split())
        assert not session.posts and not (tmp_path / "out.epub").exists()
        assert _state_file(tmp_path).read_bytes() == before and _only_job(tmp_path).state is JobState.RUNNING
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
        child.stdout.close()

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 0, result.output
    assert session.posts == ["Goodbye reader."]
    assert _chain(tmp_path)[0] == [(JobState.FAILED, "interrupted"), (JobState.VALIDATED, "validated")]


def test_a_changed_book_is_refused_and_everything_stays(tmp_path, monkeypatch):
    source = _interrupted(tmp_path, monkeypatch)
    before = _state_file(tmp_path).read_bytes()
    jobs_before = _jobs(tmp_path)
    _book(source, chapter_two="<p>Goodbye reader, changed.</p>")

    result, session = _invoke(monkeypatch, RESUME)

    text = " ".join(result.output.split())
    assert result.exit_code == 1 and "A fonte foi alterada" in text and WRAPPER in text
    assert not session.posts and not (tmp_path / "out.epub").exists()
    assert _state_file(tmp_path).read_bytes() == before and _jobs(tmp_path) == jobs_before


def test_a_missing_book_is_refused_and_everything_stays(tmp_path, monkeypatch):
    source = _interrupted(tmp_path, monkeypatch)
    before = _state_file(tmp_path).read_bytes()
    source.unlink()

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 1 and "Informe uma fonte existente" in " ".join(result.output.split()) and not session.posts
    assert _state_file(tmp_path).read_bytes() == before


def test_a_book_that_became_protected_is_told_so_before_any_translator(tmp_path, monkeypatch):
    source = _interrupted(tmp_path, monkeypatch)
    before = _state_file(tmp_path).read_bytes()
    jobs_before = _jobs(tmp_path)
    _book(source, extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 1 and "protegido" in result.output and not session.posts
    assert _state_file(tmp_path).read_bytes() == before and _jobs(tmp_path) == jobs_before


def _old_file(tmp_path, source, **changes) -> Path:
    folder = tmp_path / "processing"
    folder.mkdir(exist_ok=True)
    data = {"version": 1, "status": "running", "input_path": str(source), "output_path": str(tmp_path / "out.epub"), "cache_path": str(tmp_path / "cache.sqlite"),
            "source": "en", "target": "pt", "translator_name": "libretranslate", "url": "http://translator.test", "glossary_path": None, "fail_fast": False,
            "overwrite": False, "timeout": 30.0, "retries": 0, "chunk_limit": 3000, "created_at": "2026-09-06T11:25:49+00:00", "updated_at": "2026-09-06T11:25:49+00:00", **changes}
    path = folder / "book-pt.ayvu-state.json"
    path.write_text(json.dumps(data))
    return path


def test_an_old_file_runs_as_it_always_did_with_no_project_and_no_job(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    _old_file(tmp_path, source)

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 0, result.output
    assert session.posts and (tmp_path / "out.epub").exists()
    assert not (tmp_path / "processing" / "projects").exists()
    assert _state(tmp_path)["status"] == "completed"


def test_an_old_file_stays_byte_for_byte_when_it_is_only_read_or_when_the_translator_is_down(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    path = _old_file(tmp_path, source)
    before = path.read_bytes()

    listed, _ = _invoke(monkeypatch, ["--mode", "developer"])
    declined, _ = _invoke(monkeypatch, ["--mode", "common"], stdin="n\n")
    assert path.read_bytes() == before and "Processing translations" in listed.output and declined.exit_code in (0, 1)

    class Down(Session):
        def get(self, _url, *, timeout):
            raise __import__("requests").exceptions.ConnectionError("refused")

    down, _ = _invoke(monkeypatch, RESUME, Down())
    assert down.exit_code == 1 and path.read_bytes() == before


def test_an_old_file_with_a_project_from_before_jobs_gets_no_job(tmp_path, monkeypatch):
    """The project is real, but nothing ran under it on this plan, and the file is not given a job to fit."""
    source = _book(tmp_path / "book.epub")
    first, _ = _invoke(monkeypatch, _args(tmp_path, source))
    assert first.exit_code == 0
    count = len(_jobs(tmp_path))
    _old_file(tmp_path, source, output_path=str(tmp_path / "other.epub"))

    result, session = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "other.epub").exists() and len(_jobs(tmp_path)) == count and not session.posts


def test_an_address_with_credentials_keeps_working_on_the_current_flow(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    _old_file(tmp_path, source, url="http://user:secret@translator.test")
    result, session = _invoke(monkeypatch, RESUME)
    assert result.exit_code == 0, result.output
    assert session.posts and not (tmp_path / "processing" / "projects").exists()


@pytest.mark.parametrize("make", [
    lambda path: path.write_bytes(b"\xff\xfe"), lambda path: path.mkdir(),
], ids=["not_utf8", "directory"])
def test_a_file_that_used_to_crash_is_listed_as_invalid(tmp_path, monkeypatch, make):
    (tmp_path / "processing").mkdir()
    make(tmp_path / "processing" / "x.ayvu-state.json")

    resumed, _ = _invoke(monkeypatch, RESUME)
    assert resumed.exit_code == 1 and "No translation in progress" in resumed.output and not isinstance(resumed.exception, (UnicodeDecodeError, IsADirectoryError))

    guided, _ = _invoke(monkeypatch, ["--mode", "common"], stdin="n\n")
    assert "Invalid processing state files were found." in guided.output and "x.ayvu-state.json" in " ".join(guided.output.split())


def test_a_bad_file_does_not_stop_a_good_one(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    (tmp_path / "processing" / "bad.ayvu-state.json").write_bytes(b"\xff")
    result, session = _invoke(monkeypatch, RESUME)
    assert result.exit_code == 0, result.output
    assert session.posts == ["Goodbye reader."]


def test_a_profile_is_kept_in_the_file_finds_its_project_and_shows_nothing_new(tmp_path, monkeypatch):
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore(path).save(AyvuConfig(profiles={"tec": TranslationProfile(target_language="pt", style="technical")}))
    source = _book(tmp_path / "book.epub")
    stopped, _ = _invoke(monkeypatch, _args(tmp_path, source, "--profile", "tec"), Session(on_post=_stop_on("Goodbye")))
    assert stopped.exit_code == 1

    # A second interruption: the file the resume rewrites has to keep the profile, or the next resume loses the job.
    again, _ = _invoke(monkeypatch, RESUME, Session(on_post=_stop_on("Goodbye")))
    assert again.exit_code == 1 and _state(tmp_path)["profile_name"] == "tec"
    assert "Profile" not in again.output

    result, _ = _invoke(monkeypatch, RESUME)

    assert result.exit_code == 0, result.output
    _, jobs = _chain(tmp_path)
    assert len(jobs) == 3 and [job.continuation_of for job in jobs] == [None, jobs[0].job_id, jobs[1].job_id]


def test_failing_again_keeps_the_chain_truthful_and_the_file_running(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    stopped, _ = _invoke(monkeypatch, _args(tmp_path, source, "--fail-fast"), Session(on_post=_stop_on("Goodbye")))
    assert stopped.exit_code == 1

    result, _ = _invoke(monkeypatch, RESUME, Session(failing="Goodbye"))

    text = " ".join(result.output.split())
    assert result.exit_code == 1 and "--fail-fast" in text and WRAPPER in text and not isinstance(result.exception, ValueError)
    states, _ = _chain(tmp_path)
    assert states[0][0] is JobState.CANCELLED and states[1] == (JobState.FAILED, "provider_failed")
    assert _state(tmp_path)["status"] == "running" and not (tmp_path / "out.epub").exists()


def test_a_translator_down_refuses_like_before_and_a_later_resume_still_works(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    before = _state_file(tmp_path).read_bytes()

    class Down(Session):
        def get(self, _url, *, timeout):
            raise __import__("requests").exceptions.ConnectionError("refused")

    down, _ = _invoke(monkeypatch, RESUME, Down())
    assert down.exit_code == 1 and _state_file(tmp_path).read_bytes() == before

    result, session = _invoke(monkeypatch, RESUME)
    assert result.exit_code == 0, result.output
    assert session.posts == ["Goodbye reader."]
    assert _chain(tmp_path)[1][-1].state is JobState.VALIDATED


def test_a_file_replaced_at_the_destination_is_replaced_as_it_always_was_and_only_at_the_end(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    (tmp_path / "out.epub").write_bytes(b"not ours")
    seen = []

    def watch(_sent):
        seen.append((tmp_path / "out.epub").read_bytes())

    result, _ = _invoke(monkeypatch, RESUME, Session(on_post=watch))

    assert result.exit_code == 0, result.output
    assert seen == [b"not ours"] and (tmp_path / "out.epub").read_bytes() != b"not ours"


def test_several_interrupted_books_still_ask_which_one(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one = _book(tmp_path / "a" / "one.epub")
    two = _book(tmp_path / "b" / "two.epub", chapter_two="<p>Goodbye other.</p>")
    for source, output in ((one, tmp_path / "one-out.epub"), (two, tmp_path / "two-out.epub")):
        stopped, _ = _invoke(monkeypatch, _args(tmp_path, source, output=output), Session(on_post=_stop_on("Goodbye")))
        assert stopped.exit_code == 1

    asked, session = _invoke(monkeypatch, RESUME)
    assert asked.exit_code == 1 and "Several translations in progress" in asked.output and not session.posts

    chosen, session = _invoke(monkeypatch, [*RESUME, str(two), "--target", "pt"])
    assert chosen.exit_code == 0, chosen.output
    assert (tmp_path / "two-out.epub").exists() and not (tmp_path / "one-out.epub").exists()
    assert ResumeStateStore(tmp_path / "processing").scan().running[0].input_path == one


def test_a_job_with_no_file_is_not_shown_and_stops_nothing(tmp_path, monkeypatch):
    _interrupted(tmp_path, monkeypatch)
    _state_file(tmp_path).unlink()

    resumed, session = _invoke(monkeypatch, RESUME)
    guided, _ = _invoke(monkeypatch, ["--mode", "developer"])

    assert resumed.exit_code == 1 and "No translation in progress" in resumed.output and not session.posts
    assert "Processing translations" not in guided.output and guided.exception is None or isinstance(guided.exception, SystemExit)

