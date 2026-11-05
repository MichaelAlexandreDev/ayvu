"""Resume requests racing each other, a real killed process, and terminal races. Synchronized with events, never with sleeps."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
from datetime import timedelta

import pytest

from ayvu.cache import TranslationCache
from ayvu.domain import OutputPlan
from ayvu.job import JobContinuationError, JobRequest, JobState
from ayvu.job_recovery import CompatibilityStatus, RecoveryReason, RequiredDecision, ResumeRequest, ResumeStatus
from ayvu.job_recovery_store import _options
from ayvu.project_store import ProjectJsonRepository
from ayvu.translate_project import OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, TranslateObserver
from ayvu.translate_project_composition import (
    LegacyResources, LegacyTranslationRuntime, ProjectTranslation, open_job_recovery, open_translate_project, translate_in_project,
)
from test_cli_translate_project import _args, _book
from test_job_recovery_integration import DECIDED, Runner, Scene

CHILD = textwrap.dedent('''
    import json, os, pathlib, sys, time
    import requests
    from typer.testing import CliRunner
    import ayvu.translator
    from ayvu import cli
    class Session:
        def _response(self, payload):
            response = requests.Response()
            response.status_code = 200
            response.url = "http://translator.test"
            response._content = json.dumps(payload).encode()
            return response
        def get(self, url, *, timeout):
            return self._response([{"code": "en", "name": "E", "targets": ["pt"]}, {"code": "pt", "name": "P", "targets": []}])
        def post(self, url, *, json, timeout):
            if "Goodbye" in json["q"]:
                os.write(1, b"blocked\\n")
                while True:
                    time.sleep(60)
            return self._response({"translatedText": "PT:" + json["q"]})
        def close(self):
            pass
    ayvu.translator.requests.Session = Session
    processing = sys.argv[1]
    cli._processing_dir = lambda _config: pathlib.Path(processing)
    CliRunner().invoke(cli.app, sys.argv[2:])
''')


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def scene(tmp_path):
    return Scene(tmp_path)


class Blocked:
    """Holds the run inside a provider call until the test lets it go."""

    def __init__(self, scene, on="Goodbye"):
        self.entered, self.release = threading.Event(), threading.Event()
        scene.translator.on_call = self._call
        self.on = on

    def _call(self, text):
        if self.on in text:
            self.entered.set()
            assert self.release.wait(30)


def test_two_resume_requests_run_once_and_the_second_says_why(scene):
    state_file = scene.interrupted_state()
    state_before = state_file.read_bytes()
    entry = scene.only_entry()
    blocked = Blocked(scene)
    first = {}
    thread = threading.Thread(target=lambda: first.update(result=scene.request()))
    thread.start()
    try:
        assert blocked.entered.wait(30)
        scene.translator.on_call = None
        second = scene.request(job_id=None, expected_revision=None)
    finally:
        blocked.release.set()
        thread.join()

    assert second.status is ResumeStatus.REFUSED and second.code == "job_in_progress"
    assert first["result"].result.state is JobState.VALIDATED
    assert state_file.read_bytes() == state_before
    jobs = scene.jobs()
    assert [job.state for job in jobs] == [JobState.FAILED, JobState.VALIDATED] and jobs[1].continuation_of == entry.job_id


def test_a_request_with_what_it_saw_before_the_other_started_is_told_about_the_other(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    blocked = Blocked(scene)
    done = {}
    thread = threading.Thread(target=lambda: done.update(result=scene.request()))
    thread.start()
    try:
        assert blocked.entered.wait(30)
        scene.translator.on_call = None
        second = scene.request(job_id=entry.job_id, project_id=entry.project_id, expected_revision=entry.expected_revision)
    finally:
        blocked.release.set()
        thread.join()
    assert second.status is ResumeStatus.ALREADY_REQUESTED and second.job_id == done["result"].job_id


def test_only_one_of_two_continuations_is_created_for_one_job(scene):
    scene.save_state()
    assert scene.translate().result.state is JobState.VALIDATED
    parent = scene.jobs()[0]
    request = JobRequest(parent.request.project_id, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, parent.request.input_fingerprint, (OUTPUT_ROLE,))
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt():
        repository = scene.jobs_repository()
        barrier.wait(30)
        try:
            outcomes.append(repository.create_continuation(parent.job_id, request).job_id)
        except JobContinuationError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("continuation_exists") == 1 and len(scene.jobs()) == 2


def test_a_job_settled_by_someone_else_after_the_look_stops_the_resume(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    recovery = scene.recovery()
    scene.clock.ahead = timedelta(hours=2)
    scene.translate()  # Another run of this plan settles the old job and takes the newest place.

    result = recovery.request_resume.execute(
        ResumeRequest(entry.reference, entry.job_id, None, entry.expected_revision, DECIDED), Runner(scene))

    assert result.status is ResumeStatus.REFUSED and result.code == "stale_revision"
    assert [job.continuation_of for job in scene.jobs()] == [None, None]


def test_a_job_settled_in_the_instant_before_the_run_is_stale_for_the_run_itself(scene):
    scene.interrupted_state()
    scene.clock.ahead = timedelta(hours=2)

    class SettlesFirst(Runner):
        def run_durable(self, reference, ticket, operation_id):
            project = scene.project()
            runtime = LegacyTranslationRuntime(scene.source, scene.options, lambda: LegacyResources(scene.translator), lambda: TranslationCache(scene.cache))
            open_translate_project(scene.processing, project.project_id, scene.source, scene.output, overwrite=True, runtime=runtime, clock=scene.clock).reconcile(project.project_id)
            return super().run_durable(reference, ticket, operation_id)

    result = scene.request(runner=SettlesFirst(scene))

    assert result.status is ResumeStatus.REFUSED and result.code == "stale_revision"
    assert len(scene.jobs()) == 1 and scene.jobs()[0].state is JobState.FAILED


def test_a_reader_probing_the_guard_does_not_refuse_a_run_starting(scene, monkeypatch):
    import fcntl

    scene.save_state()
    project = scene.project()
    guard = scene.projects.run_guard(project.project_id)
    fingerprint = "sha256:" + "c" * 64
    with guard.exclusive(fingerprint) as alone:
        assert alone
    lock = guard._path(fingerprint)
    reader = os.open(lock, os.O_RDONLY)
    fcntl.flock(reader, fcntl.LOCK_SH)

    def reader_finishes(_seconds):
        fcntl.flock(reader, fcntl.LOCK_UN)

    monkeypatch.setattr("ayvu.project_store.time.sleep", reader_finishes)
    try:
        with guard.exclusive(fingerprint) as alone:
            assert alone
    finally:
        os.close(reader)


def _kill_a_real_run(tmp_path):
    source = _book(tmp_path / "book.epub")
    arguments = _args(tmp_path, source)
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "processing"), *arguments], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment, cwd=tmp_path)
    assert child.stdout.readline() == b"blocked\n"
    return child, source


def test_a_real_kill_is_seen_while_alive_and_after_and_resumes_with_the_file_it_left(tmp_path):
    child, source = _kill_a_real_run(tmp_path)
    recovery = open_job_recovery(tmp_path / "processing")
    try:
        alive = recovery.list_interrupted.execute().entries[0]
        assert alive.compatibility is CompatibilityStatus.DURABLE and RecoveryReason.ACTIVE_LEASE in alive.reasons and not alive.resumable
        before = alive
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
        child.stdout.close()

    dead = recovery.list_interrupted.execute().entries[0]
    assert dead.job_id == before.job_id and RecoveryReason.LEASE_OWNER_GONE in dead.reasons and dead.resumable
    inspection = recovery.inspect.execute(dead.reference)
    assert inspection.required_decisions == (RequiredDecision.FULL_REEXECUTION,) and inspection.provider_calls_may_repeat

    calls = []

    class Fake:
        def translate(self, text, source, target):
            calls.append(text)
            return f"PT:{text}"

    class FromTheFile:
        def run_durable(self, reference, ticket, operation_id):
            state = recovery.evidence.legacy_entry(reference).state
            project = ProjectJsonRepository(tmp_path / "processing").load(dead.project_id)
            return translate_in_project(ProjectTranslation(
                tmp_path / "processing", project.project_id, project.source_sha256, state.input_path, _options(state), OutputPlan(path=state.output_path), True,
                lambda: LegacyResources(Fake()), lambda: TranslationCache(state.cache_path), ticket, operation_id), TranslateObserver()).result

        def run_legacy(self, reference):
            raise AssertionError("this one is durable")

    result = recovery.request_resume.execute(
        ResumeRequest(dead.reference, dead.job_id, dead.project_id, dead.expected_revision, inspection.required_decisions), FromTheFile())

    assert result.status is ResumeStatus.STARTED and result.result.state is JobState.VALIDATED
    assert calls == ["Goodbye reader."]
    assert (tmp_path / "out.epub").exists() and source.exists()
