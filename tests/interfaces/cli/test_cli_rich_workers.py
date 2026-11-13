"""Labeled online workers: real stores, real concurrency and one production per repeated text."""
from threading import Barrier, BrokenBarrierError, Lock, get_ident
from zipfile import ZipFile

import pytest

from ayvu.jobs import JobState
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.rich_preview_support import preview_book


class Sessions:
    """A new instrumented session per translator, so ownership is observable."""

    def __init__(self, **kwargs):
        self.kwargs, self.created, self.lock = kwargs, [], Lock()

    def __call__(self):
        session = Transport(**self.kwargs)
        session.closed = 0
        session.close = lambda session=session: setattr(session, "closed", session.closed + 1)
        with self.lock:
            self.created.append(session)
        return session

    @property
    def posts(self):
        return [post for session in self.created for post in session.posts]


def run(tmp_path, monkeypatch, source, *arguments, sessions=None, output=None):
    sessions = sessions or Sessions()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", sessions)
    from tests.support.cli_rich_runtime_support import runner
    from tests.support.cli_translate_project_support import _args
    from ayvu.interfaces.cli import app as cli
    result = runner.invoke(cli.app, _args(tmp_path, source, *arguments, output=output))
    return result, sessions


def members(path):
    with ZipFile(path) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


@pytest.mark.parametrize("arguments,workers", [(["--workers", "2"], 2), (["--workers", "4"], 4),
                                               (["--performance-profile", "high"], 4)])
def test_online_workers_match_the_single_worker_output(tmp_path, monkeypatch, arguments, workers):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    single, _ = run(tmp_path, monkeypatch, source, "--workers", "1", "--cache", str(tmp_path / "one.sqlite"),
                    output=tmp_path / "one.epub")
    assert single.exit_code == 0, text(single)
    result, sessions = run(tmp_path, monkeypatch, source, *arguments, "--cache", str(tmp_path / "many.sqlite"),
                           output=tmp_path / "many.epub")
    assert result.exit_code == 0, text(result)
    assert any(line.split("│")[1:3] and line.split("│")[1].strip() == "Workers"
               and line.split("│")[2].strip().startswith(str(workers)) for line in text(result).splitlines())
    assert members(tmp_path / "many.epub") == members(tmp_path / "one.epub")
    job = _jobs(tmp_path)[-1]
    assert job.state is JobState.VALIDATED and job.request.command_version == 2
    # The discovery session plus one per document task; every one of them is closed.
    assert len(sessions.created) > 1 and all(session.closed for session in sessions.created)


def test_two_documents_are_in_flight_at_the_same_time(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    barrier, lock, met = Barrier(2, timeout=10), Lock(), []

    def meet(text_value):
        if not text_value.startswith("Document"):
            return  # The title runs on the coordinator before any chapter.
        with lock:
            first_two = len(met) < 2
            if first_two:
                met.append(get_ident())
        if first_two:
            try:
                barrier.wait()  # Both requests must be blocked here at once.
            except BrokenBarrierError:
                pytest.fail("the second document never started while the first was blocked")

    result, _ = run(tmp_path, monkeypatch, source, "--workers", "2", sessions=Sessions(on_post=meet))
    assert result.exit_code == 0, text(result)
    assert len(set(met)) == 2


def test_repeated_text_is_produced_once_across_workers(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub", repeated=True)
    result, sessions = run(tmp_path, monkeypatch, source, "--workers", "4")
    assert result.exit_code == 0, text(result)
    assert sessions.posts.count("Repeated reader.") == 1
    bodies = [value for name, value in members(tmp_path / "out.epub").items() if name.startswith("OEBPS/c") and name.endswith(".xhtml")]
    assert len(bodies) == 13 and all(b"PT:Repeated reader." in body for body in bodies)


def test_every_worker_shares_one_rate_limiter(tmp_path, monkeypatch):
    from ayvu.providers.libretranslate import adapter
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    limiters, original = [], adapter.LibreTranslateProvider.__init__

    def record(self, translator, *args, **kwargs):
        base = getattr(translator, "_base", translator)
        limiters.append(base.rate_limiter)
        original(self, translator, *args, **kwargs)

    monkeypatch.setattr(adapter.LibreTranslateProvider, "__init__", record)
    result, _ = run(tmp_path, monkeypatch, source, "--workers", "4", "--requests-per-second", "1000")
    assert result.exit_code == 0, text(result)
    assert len(limiters) > 2 and len({id(limiter) for limiter in limiters}) == 1
    assert limiters[0].requests_per_second == 1000


def test_a_provider_failure_stops_the_book_without_publication(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    result, sessions = run(tmp_path, monkeypatch, source, "--workers", "4", sessions=Sessions(failing="Document 05"))
    assert result.exit_code == 1, text(result)
    assert not (tmp_path / "out.epub").exists()
    job = _jobs(tmp_path)[-1]
    assert job.state is JobState.FAILED
    assert all(session.closed for session in sessions.created)


def test_parallel_governed_memory_is_still_refused_before_any_request(tmp_path, monkeypatch):
    config(tmp_path)
    result, sessions = run(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--workers", "2", "--translation-memory")
    assert result.exit_code == 1 and "incompatível" in text(result)
    assert sessions.posts == []


@pytest.mark.parametrize("workers", ["2", "4"])
def test_workers_wait_out_a_slow_writer_on_the_project_database(tmp_path, monkeypatch, workers):
    """A one-second write lock, as a slow disk under load produces, must not fail the book."""
    import sqlite3
    import time
    from threading import Event, Thread
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    held, done, once = Event(), Event(), Lock()

    def hold_write_lock():
        database = next((tmp_path / "processing").rglob("project.sqlite"))
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(1.0)
        connection.execute("COMMIT")
        connection.close()
        done.set()

    def on_post(text_value):
        if not text_value.startswith("Document"):
            return
        if once.acquire(blocking=False):  # Exactly one external writer, whichever worker posts first.
            Thread(target=hold_write_lock, daemon=True).start()
        assert held.wait(5)

    result, _ = run(tmp_path, monkeypatch, source, "--workers", workers, sessions=Sessions(on_post=on_post))
    assert done.wait(5)
    assert result.exit_code == 0, text(result)
    assert _jobs(tmp_path)[-1].state is JobState.VALIDATED


def test_four_workers_survive_a_busy_project_database(tmp_path, monkeypatch):
    """Another writer holding the database 0.6 s at a time, as a slow disk under load does.

    Workers take turns in-process, so SQLite's busy timeout only ever covers one worker's
    write next to the coordinator's. Without the turns, the queue outlasts it.
    """
    import sqlite3
    import time
    from threading import Event, Thread
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    stop, started, once = Event(), Event(), Lock()

    def busy_disk():
        database = next((tmp_path / "processing").rglob("project.sqlite"))
        connection = sqlite3.connect(database, isolation_level=None, timeout=10)
        while not stop.is_set():
            connection.execute("BEGIN IMMEDIATE")
            started.set()
            time.sleep(0.6)
            connection.execute("COMMIT")
            time.sleep(0.05)
        connection.close()

    def on_post(text_value):
        if text_value.startswith("Document") and once.acquire(blocking=False):
            Thread(target=busy_disk, daemon=True).start()
            assert started.wait(5)

    try:
        result, _ = run(tmp_path, monkeypatch, source, "--workers", "4", sessions=Sessions(on_post=on_post))
    finally:
        stop.set()
    assert result.exit_code == 0, text(result)
    assert _jobs(tmp_path)[-1].state is JobState.VALIDATED
