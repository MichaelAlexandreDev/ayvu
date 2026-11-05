"""Recovery over the production stores, the real state files and the real CLI, under a fake translator.

A "crash" here is an exception nothing in production catches, raised from the translator. It leaves a job running
with a live lease and a guard nobody holds, as a killed process does. A real kill is in the concurrency tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ayvu.cache import TranslationCache
from ayvu.cli import EXISTING_OUTPUT_OVERWRITE_OPTION, app
from ayvu.config import AyvuConfig, ConfigStore, TranslationProfile, default_config_path
from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions
from ayvu.job import JobRequest, JobState
from ayvu.job_recovery import (
    CompatibilityStatus, RecoveryReason, RequiredDecision, ResumeRequest, ResumeStage, ResumeStatus,
)
from ayvu.legacy_resume import MAX_LEGACY_STATE_BYTES, scan_legacy_states
from ayvu.project_schema import PROJECT_MIGRATIONS, open_job_repository
from ayvu.project_store import ProjectJsonRepository, build_project_request, translation_selections
from ayvu.resume import ResumeStateStore, TranslationResumeState
from ayvu.sqlite_migrations import SqliteMigrationRunner
from ayvu.translate_project import (
    OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, ContinuationTicket, TranslateObserver, TranslateProjectCommand,
)
from ayvu.translate_project_composition import (
    LegacyResources, LegacyTranslationRuntime, ProjectTranslation, open_job_recovery, open_translate_project, translate_in_project,
)
from test_cli_translate_project import ENCRYPTED_DOCUMENT, Session, _args, _book

PAIR = LanguagePair(source="en", target="pt")
CANARY_URL = "http://canary.example.test:5000"
DECIDED = (RequiredDecision.FULL_REEXECUTION, RequiredDecision.REPLACE_DESTINATION)


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.cli._processing_dir", lambda _config: tmp_path / "processing")


class Crash(BaseException):
    """Nothing in production catches this one."""


class Clock:
    """Real time, so that jobs keep their order, with a way to jump past a lease."""

    def __init__(self):
        self.ahead = timedelta()

    def __call__(self):
        return datetime.now(UTC) + self.ahead


class Prefix:
    def __init__(self):
        self.calls = []
        self.on_call = None

    def translate(self, text, source, target):
        self.calls.append(text)
        if self.on_call is not None:
            self.on_call(text)
        return f"PT:{text}"


def tree(root: Path) -> dict[str, tuple]:
    """Everything a write could change: names, bytes, times and modes."""
    found = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        found[str(path.relative_to(root))] = (content, info.st_mtime_ns, info.st_mode)
    return found


class Scene:
    """One book, its project, its state file and its output, as the CLI leaves them."""

    def __init__(self, tmp_path: Path, *, source=None, output=None, profile=None, url=CANARY_URL) -> None:
        self.tmp_path = tmp_path
        self.processing = tmp_path / "processing"
        self.source = source or _book(tmp_path / "book.epub")
        self.output = output or tmp_path / "out.epub"
        self.cache = tmp_path / "cache.sqlite"
        self.clock = Clock()
        self.translator = Prefix()
        self.options = TranslationOptions(language_pair=PAIR)
        self.profile = profile
        self.url = url
        self.projects = ProjectJsonRepository(self.processing)

    def selections(self):
        return translation_selections(translator="libretranslate", url=self.url, chunk_limit=self.options.chunk_limit,
                                      translate_metadata=False, translate_alt_text=False, chapters=None, translation_memory=None)

    def project(self):
        return self.projects.resolve(build_project_request(self.source, source="en", target="pt", selections=self.selections(), profile_name=self.profile)).project

    def save_state(self, **changes) -> Path:
        state = TranslationResumeState.create(self.source, self.output, self.cache, "libretranslate", self.url, None, self.options, False, 30.0, 0,
                                              profile_name=self.profile)
        return ResumeStateStore(self.processing).save(replace(state, **changes))

    def translate(self, *, overwrite=False, continuation=None, operation_id=None):
        project = self.project()
        return translate_in_project(ProjectTranslation(
            self.processing, project.project_id, project.source_sha256, self.source, self.options, OutputPlan(path=self.output), overwrite,
            lambda: LegacyResources(self.translator), lambda: TranslationCache(self.cache), continuation, operation_id), TranslateObserver())

    def crash(self, *, overwrite=False, on="Goodbye", continuation=None):
        def stop(text):
            if on in text:
                self.translator.on_call = None
                raise Crash
        self.translator.on_call = stop
        with pytest.raises(Crash):
            self.translate(overwrite=overwrite, continuation=continuation)

    def interrupted_state(self, **crash) -> Path:
        path = self.save_state()
        self.crash(**crash)
        return path

    def recovery(self, **changes):
        return open_job_recovery(self.processing, clock=self.clock, **changes)

    def jobs_repository(self):
        project = self.project()
        paths = self.projects.resolve_database_paths(project.project_id)
        return open_job_repository(paths.database, paths.backups, project.project_id, clock=self.clock)

    def jobs(self):
        return [item.job for item in self.jobs_repository().list_jobs(self.project().project_id)]

    def only_entry(self, recovery=None):
        entries = (recovery or self.recovery()).list_interrupted.execute().entries
        assert len(entries) == 1, entries
        return entries[0]

    def request(self, runner=None, recovery=None, **changes):
        recovery = recovery or self.recovery()
        entry = recovery.list_interrupted.execute().entries[0]
        fields = dict(reference=entry.reference, job_id=entry.job_id, project_id=entry.project_id, expected_revision=entry.expected_revision, accepted=DECIDED)
        return recovery.request_resume.execute(ResumeRequest(**{**fields, **changes}), runner or Runner(self))


class Runner:
    """What a consumer of the use case provides. The CLI's own arrives with the migration of its commands."""

    def __init__(self, scene: Scene) -> None:
        self.scene = scene
        self.legacy = []

    def run_durable(self, reference, ticket, operation_id):
        return self.scene.translate(overwrite=True, continuation=ticket, operation_id=operation_id).result

    def run_legacy(self, reference):
        self.legacy.append(reference)


@pytest.fixture
def scene(tmp_path):
    return Scene(tmp_path)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("no network here")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr("requests.Session", refuse)


# ---------- Reading changes nothing.

def test_listing_and_inspecting_change_nothing_and_reach_nothing(scene, no_network):
    scene.interrupted_state()
    before = tree(scene.tmp_path)
    recovery = scene.recovery()

    entry = recovery.list_interrupted.execute().entries[0]
    seen = recovery.inspect.execute(entry.reference)

    assert seen.entry.resumable and tree(scene.tmp_path) == before
    assert scene.translator.calls.count("Goodbye reader.") == 1


def test_reading_an_empty_or_absent_folder_creates_nothing(tmp_path, no_network):
    recovery = open_job_recovery(tmp_path / "absent")
    assert recovery.list_interrupted.execute().entries == ()
    assert not (tmp_path / "absent").exists()
    (tmp_path / "empty").mkdir()
    open_job_recovery(tmp_path / "empty").list_interrupted.execute()
    assert list((tmp_path / "empty").iterdir()) == []


def test_reading_a_project_with_an_older_schema_does_not_upgrade_it(scene):
    scene.save_state()
    project = scene.project()
    paths = scene.projects.resolve_database_paths(project.project_id)
    SqliteMigrationRunner(PROJECT_MIGRATIONS[:3]).migrate(paths.database, paths.backups)
    before = tree(scene.processing)

    entries = scene.recovery().list_interrupted.execute().entries

    assert tree(scene.processing) == before
    assert {RecoveryReason.UNSUPPORTED_SCHEMA} <= {reason for entry in entries for reason in entry.reasons}


def test_a_project_with_no_database_yet_has_no_jobs(scene):
    scene.save_state()
    scene.project()
    assert scene.only_entry().compatibility is CompatibilityStatus.LEGACY_ONLY


def test_one_bad_project_does_not_hide_the_others(scene):
    scene.interrupted_state()
    bad = scene.processing / "projects" / "11111111-1111-4111-8111-111111111111"
    bad.mkdir()
    (bad / "project.json").write_text("{not json")
    (scene.processing / "projects" / ".staging-abc").mkdir()

    listing = scene.recovery().list_interrupted.execute()

    assert listing.unreadable_projects == 2
    assert [entry.compatibility for entry in listing.entries] == [CompatibilityStatus.DURABLE]


# ---------- Finding the job from the file.

def _running_state(tmp_path):
    return next(item for item in scan_legacy_states(tmp_path / "processing"))


@pytest.mark.parametrize("more", [
    (), ("--chunk-limit", "500"), ("--chapters", "1-2"), ("--translate-metadata",), ("--translate-alt-text",), ("--translation-memory",), ("--fail-fast",),
    ("--overwrite",),
], ids=["plain", "chunk_limit", "chapters", "metadata", "alt_text", "memory", "fail_fast", "overwrite_flag"])
def test_an_interrupted_run_of_the_real_cli_is_found_from_its_file(tmp_path, monkeypatch, more):
    source = _book(tmp_path / "book.epub")
    if "--overwrite" in more:
        (tmp_path / "out.epub").write_bytes(b"old")
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, *more))
    assert entry.compatibility is CompatibilityStatus.DURABLE and entry.job_state is JobState.CANCELLED


def _interrupt_real_cli(tmp_path, monkeypatch, arguments, stdin=None):
    def stop(text):
        if "Goodbye" in text:
            raise KeyboardInterrupt
    session = Session(on_post=stop)
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: session)
    result = CliRunner().invoke(app, arguments, input=stdin)
    assert result.exit_code == 1, result.output
    return open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]


def test_a_relative_output_is_found_from_its_file(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    arguments = _args(tmp_path, source, output=Path("relative-out.epub"))
    assert _interrupt_real_cli(tmp_path, monkeypatch, arguments).compatibility is CompatibilityStatus.DURABLE


def test_an_output_prompt_accepted_is_found_from_its_file(tmp_path, monkeypatch):
    """The file records the flag, which stayed off, and the plan holds what the person accepted at the prompt."""
    source = _book(tmp_path / "book.epub")
    (tmp_path / "out.epub").write_bytes(b"old")
    entry = _interrupt_real_cli(tmp_path, monkeypatch, ["--mode", "common", *_args(tmp_path, source)[2:]], stdin=f"{EXISTING_OUTPUT_OVERWRITE_OPTION}\n")
    assert entry.compatibility is CompatibilityStatus.DURABLE
    assert json.loads(next((tmp_path / "processing").glob("*.ayvu-state.json")).read_text())["overwrite"] is False


def test_a_profile_is_found_only_from_a_file_that_records_it(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore(path).save(AyvuConfig(profiles={"tec": TranslationProfile(target_language="pt", style="technical")}))
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, "--profile", "tec"))
    assert entry.compatibility is CompatibilityStatus.DURABLE
    state_file = next((tmp_path / "processing").glob("*.ayvu-state.json"))
    data = json.loads(state_file.read_text())
    assert data["profile_name"] == "tec"
    del data["profile_name"]
    state_file.write_text(json.dumps(data))
    assert open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0].compatibility is CompatibilityStatus.LEGACY_ONLY


def test_a_changed_glossary_is_a_different_configuration_and_a_missing_one_is_said(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    glossary = tmp_path / "g.json"
    glossary.write_text('{"Hello": "Ola"}')
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, "--glossary", str(glossary)))
    assert entry.compatibility is CompatibilityStatus.DURABLE
    glossary.write_text('{"Hello": "Oi"}')
    changed = open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]
    assert changed.compatibility is CompatibilityStatus.LEGACY_ONLY and changed.job_id is None
    glossary.unlink()
    gone = open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]
    assert gone.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.MISSING_CONFIGURATION in gone.reasons


def test_an_address_holding_credentials_stays_on_the_legacy_path(tmp_path):
    scene = Scene(tmp_path, url="http://user:secret@translator.test")
    scene.save_state()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.MISSING_CONFIGURATION in entry.reasons
    assert "secret" not in repr(entry)


def test_a_file_that_finished_is_not_offered(scene):
    path = scene.save_state()
    state = ResumeStateStore(scene.processing).load(path)
    ResumeStateStore(scene.processing).save(state.mark_completed())
    assert scene.recovery().list_interrupted.execute().entries == ()


def test_two_books_with_one_name_share_a_file_and_the_first_is_kept_as_a_job(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = Scene(tmp_path, source=_book(tmp_path / "a" / "book.epub"), output=tmp_path / "a.epub")
    second = Scene(tmp_path, source=_book(tmp_path / "b" / "book.epub", chapter_two="<p>Goodbye other.</p>"), output=tmp_path / "b.epub")
    first.interrupted_state()
    second.interrupted_state()

    entries = first.recovery().list_interrupted.execute().entries

    assert sorted(entry.compatibility for entry in entries) == [CompatibilityStatus.DURABLE, CompatibilityStatus.JOB_ONLY]


# ---------- Resuming.

def test_a_killed_run_resumes_as_a_linked_job_and_repeats_only_what_is_missing(scene):
    state_file = scene.interrupted_state()
    before = state_file.read_bytes()
    scene.translator.calls.clear()

    result = scene.request()

    assert result.status is ResumeStatus.STARTED and result.result.state is JobState.VALIDATED
    assert scene.translator.calls == ["Goodbye reader."]
    parent, child = scene.jobs()
    assert (parent.state, parent.terminal_summary.reason_code) == (JobState.FAILED, "interrupted")
    assert child.continuation_of == parent.job_id and child.state is JobState.VALIDATED
    assert scene.output.exists() and state_file.read_bytes() == before


def test_the_repeated_call_is_disclosed_and_the_cache_is_not_called_proof(scene):
    scene.interrupted_state()
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert seen.provider_calls_may_repeat and RequiredDecision.FULL_REEXECUTION in seen.required_decisions
    assert seen.selection.resume_from is ResumeStage.TRANSLATE and seen.selection.reusable == ()
    assert RecoveryReason.PROVENANCE_UNPROVEN in seen.entry.reasons


def test_a_finished_run_is_not_proved_reusable_either(scene):
    scene.save_state()
    assert scene.translate().result.state is JobState.VALIDATED
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert seen.selection.resume_from is ResumeStage.TRANSLATE and seen.selection.reason is RecoveryReason.ARTIFACT_MISSING
    assert RecoveryReason.PROVENANCE_UNPROVEN in seen.entry.reasons
    assert RecoveryReason.TERMINAL_JOB in seen.entry.reasons


def test_ctrl_c_resumes_as_a_linked_job_without_reopening_the_cancelled_one(scene):
    scene.save_state()
    def stop(text):
        if "Goodbye" in text:
            scene.translator.on_call = None
            raise KeyboardInterrupt
    scene.translator.on_call = stop
    with pytest.raises(KeyboardInterrupt):
        scene.translate()
    cancelled = scene.jobs()[0]
    assert cancelled.state is JobState.CANCELLED

    result = scene.request()

    assert result.result.state is JobState.VALIDATED
    assert scene.jobs()[0].state is JobState.CANCELLED and scene.jobs()[0].revision == cancelled.revision
    assert scene.jobs()[1].continuation_of == cancelled.job_id


def test_an_expired_lease_is_reconciled_and_continued(scene):
    scene.interrupted_state()
    scene.clock.ahead = timedelta(hours=2)
    entry = scene.only_entry()
    assert RecoveryReason.EXPIRED_LEASE in entry.reasons and entry.resumable
    assert scene.request().result.state is JobState.VALIDATED
    assert scene.jobs()[0].terminal_summary.reason_code == "interrupted"


def test_a_resume_of_a_resume_takes_each_guard_once(scene):
    """The continuation runs with overwrite on, as does a resume of it, so parent and child share a plan."""
    scene.interrupted_state()
    ticket_request = scene.only_entry()
    scene.crash(overwrite=True, continuation=ContinuationTicket(ticket_request.job_id, ticket_request.expected_revision), on="Goodbye")
    parent, middle = scene.jobs()
    assert parent.request.input_fingerprint != middle.request.input_fingerprint

    result = scene.request()

    assert result.status is ResumeStatus.STARTED and result.result.state is JobState.VALIDATED
    chain = scene.jobs()
    assert [job.continuation_of for job in chain] == [None, parent.job_id, middle.job_id]
    assert chain[1].request.input_fingerprint == chain[2].request.input_fingerprint


def test_a_live_run_is_seen_as_active_and_blocks_a_resume(scene):
    scene.save_state()
    outcome = {}

    def while_running(text):
        if "Goodbye" in text and not outcome:
            recovery = scene.recovery()
            entry = recovery.list_interrupted.execute().entries[0]
            outcome["entry"] = entry
            outcome["resume"] = scene.request(recovery=recovery)
    scene.translator.on_call = while_running

    finished = scene.translate()

    entry, resume = outcome["entry"], outcome["resume"]
    assert RecoveryReason.ACTIVE_LEASE in entry.reasons and not entry.resumable
    assert resume.status is ResumeStatus.REFUSED and resume.code == "job_in_progress"
    assert finished.result.state is JobState.VALIDATED and len(scene.jobs()) == 1


def test_a_resume_racing_the_run_is_refused_by_the_guard_itself(scene):
    """Between the look and the run the original comes back: the parent's guard is what stops it."""
    scene.interrupted_state()
    entry = scene.only_entry()
    project = scene.project()
    guard = scene.projects.run_guard(project.project_id)
    with guard.exclusive(scene.jobs()[0].request.input_fingerprint) as alone:
        assert alone
        run = scene.translate(overwrite=True, continuation=ContinuationTicket(entry.job_id, entry.expected_revision))
    assert run.result.failure.code == "job_in_progress" and run.result.job_id is None
    assert [job.state for job in scene.jobs()] == [JobState.RUNNING]


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_a_source_that_is_not_as_recorded_refuses_and_leaves_everything(scene, change):
    state_file = scene.interrupted_state()
    state_before = state_file.read_bytes()
    if change == "changed":
        _book(scene.source, chapter_two="<p>Goodbye reader, changed.</p>")
    else:
        scene.source.unlink()
    recovery = scene.recovery()
    entry = recovery.list_interrupted.execute().entries[0]
    seen = recovery.inspect.execute(entry.reference)
    assert not seen.entry.resumable
    assert (RecoveryReason.SOURCE_CHANGED if change == "changed" else RecoveryReason.SOURCE_MISSING) in seen.entry.reasons

    result = recovery.request_resume.execute(ResumeRequest(entry.reference, accepted=DECIDED), Runner(scene))

    assert result.status is ResumeStatus.REFUSED and result.code == f"source_{change}"
    assert state_file.read_bytes() == state_before and not scene.output.exists()


def test_policy_refuses_before_the_parent_is_touched(scene):
    state_file = scene.interrupted_state()
    state_before = state_file.read_bytes()
    jobs_before = scene.jobs()
    recovery = scene.recovery(policy_refusal=lambda _path: "encrypted_content")

    result = scene.request(recovery=recovery)

    assert result.status is ResumeStatus.REFUSED and result.code == "policy_denied"
    assert scene.jobs() == jobs_before and state_file.read_bytes() == state_before and not scene.output.exists()


def test_the_real_policy_check_sees_a_protected_book_on_the_legacy_path(tmp_path):
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))
    scene = Scene(tmp_path, source=source)
    scene.save_state()
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert RecoveryReason.POLICY_DENIED in seen.entry.reasons


def test_a_job_with_no_file_cannot_be_resumed_and_says_why(scene):
    state_file = scene.interrupted_state()
    state_file.unlink()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.JOB_ONLY and RecoveryReason.INVOCATION_UNAVAILABLE in entry.reasons
    result = scene.request(reference=entry.reference)
    assert result.status is ResumeStatus.REFUSED and result.code == "invocation_unavailable" and len(scene.jobs()) == 1


def test_a_request_repeated_after_finishing_runs_nothing_and_names_the_job(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    first = scene.request()
    calls = list(scene.translator.calls)

    again = scene.recovery().request_resume.execute(
        ResumeRequest(entry.reference, entry.job_id, entry.project_id, entry.expected_revision, DECIDED), Runner(scene))

    assert again.status is ResumeStatus.ALREADY_REQUESTED and again.job_id == first.job_id
    assert scene.translator.calls == calls and len(scene.jobs()) == 2


def test_a_stale_ticket_creates_nothing(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    run = scene.translate(overwrite=True, continuation=ContinuationTicket(entry.job_id, entry.expected_revision + 1))
    assert run.result.failure.code == "stale_revision" and len(scene.jobs()) == 1


def test_a_continuation_left_queued_by_a_crash_does_not_block_the_next_resume(scene):
    scene.interrupted_state()
    parent = scene.jobs()[0]
    scene.request()  # A complete chain first, so that the queued one is on the same plan as what follows.
    repository = scene.jobs_repository()
    chain_end = repository.list_jobs(parent.request.project_id, limit=8, newest_first=True)[0].job
    stuck = repository.create_continuation(chain_end.job_id, JobRequest(
        parent.request.project_id, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, chain_end.request.input_fingerprint, (OUTPUT_ROLE,)))
    scene.save_state()

    result = scene.request()

    assert result.result.state is JobState.VALIDATED
    settled = repository.get(stuck.job_id).job
    assert (settled.state, settled.terminal_summary.reason_code) == (JobState.CANCELLED, "interrupted")
    assert scene.jobs()[-1].continuation_of == stuck.job_id


def test_cancelling_a_resumed_run_leaves_a_truthful_state_that_can_resume_again(scene):
    scene.interrupted_state()
    entry = scene.only_entry()
    project = scene.project()
    runtime = LegacyTranslationRuntime(scene.source, scene.options, lambda: LegacyResources(scene.translator), lambda: TranslationCache(scene.cache))
    use_case = open_translate_project(scene.processing, project.project_id, scene.source, scene.output, overwrite=True, runtime=runtime, clock=scene.clock)
    cancellation = use_case.cancellation()
    cancellation.request("user_requested", scene.clock())
    result = use_case.execute(TranslateProjectCommand(project.project_id, project.source_sha256, scene.options, OutputPlan(path=scene.output.resolve()), overwrite=True),
                              cancellation=cancellation, continuation=ContinuationTicket(entry.job_id, entry.expected_revision))
    assert result.state is JobState.CANCELLED
    jobs = scene.jobs()
    assert [job.state for job in jobs] == [JobState.FAILED, JobState.CANCELLED] and jobs[1].continuation_of == jobs[0].job_id

    assert scene.request().result.state is JobState.VALIDATED
    assert scene.jobs()[2].continuation_of == jobs[1].job_id


def test_cancelling_a_long_read_writes_nothing_and_decides_nothing(scene):
    scene.interrupted_state()
    before = tree(scene.tmp_path)
    recovery = scene.recovery()
    entry = recovery.list_interrupted.execute().entries[0]
    cancellation = recovery.inspect.cancellation()
    cancellation.request("user_requested", scene.clock())

    seen = recovery.inspect.execute(entry.reference, cancellation=cancellation)

    assert seen.cancelled and seen.required_decisions == () and tree(scene.tmp_path) == before


# ---------- The output.

def _finished(scene):
    scene.save_state()
    assert scene.translate().result.state is JobState.VALIDATED


def test_an_intact_output_is_verified_from_its_bytes(scene):
    _finished(scene)
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert not {RecoveryReason.ARTIFACT_MISSING, RecoveryReason.ARTIFACT_PARTIAL, RecoveryReason.ARTIFACT_CORRUPT, RecoveryReason.DESTINATION_CONFLICT} & set(seen.entry.reasons)
    assert RequiredDecision.REPLACE_DESTINATION in seen.required_decisions


@pytest.mark.parametrize("damage, reason", [
    (lambda path: path.unlink(), RecoveryReason.ARTIFACT_MISSING),
    (lambda path: path.write_bytes(path.read_bytes()[:-5]), RecoveryReason.ARTIFACT_PARTIAL),
    (lambda path: path.write_bytes(b"Z" + path.read_bytes()[1:]), RecoveryReason.ARTIFACT_CORRUPT),
], ids=["missing", "partial", "corrupt"])
def test_a_damaged_output_has_its_own_reason_and_can_be_redone(scene, damage, reason):
    _finished(scene)
    damage(scene.output)
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert reason in seen.entry.reasons and seen.entry.resumable
    assert scene.request().result.state is JobState.VALIDATED


def test_a_file_at_the_destination_with_no_record_is_a_conflict_that_needs_a_decision(scene):
    scene.interrupted_state()
    scene.output.write_bytes(b"not ours")
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert RecoveryReason.DESTINATION_CONFLICT in seen.entry.reasons and RequiredDecision.REPLACE_DESTINATION in seen.required_decisions
    refused = scene.request(accepted=(RequiredDecision.FULL_REEXECUTION,))
    assert refused.code == "decision_required" and scene.output.read_bytes() == b"not ours"


# ---------- Old files.

V1_FILE = {
    "version": 1, "status": "running", "translator_name": "libretranslate", "url": CANARY_URL, "glossary_path": None, "fail_fast": False,
    "overwrite": False, "timeout": 30.0, "retries": 0, "chunk_limit": 3000, "source": "en", "target": "pt",
    "created_at": "2026-09-06T11:25:49+00:00", "updated_at": "2026-09-06T11:25:49+00:00",
    "completed_chapters": ["c1.xhtml"], "failed_chapters": ["c2.xhtml"], "failed_segment_count": 2, "total_chapters": 3,
}


def _v1(tmp_path, **changes):
    return {**V1_FILE, "input_path": str(tmp_path / "book.epub"), "output_path": str(tmp_path / "out.epub"), "cache_path": str(tmp_path / "cache.sqlite"), **changes}


def _write(tmp_path, name, content):
    folder = tmp_path / "processing"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return path


def test_an_old_file_is_inspectable_gets_no_identity_and_is_not_touched(scene, tmp_path, no_network):
    path = _write(tmp_path, "book-pt.ayvu-state.json", _v1(tmp_path))
    before = tree(tmp_path)

    entry = scene.only_entry()
    seen = scene.recovery().inspect.execute(entry.reference)

    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and entry.reasons == (RecoveryReason.LEGACY_ONLY,)
    assert (entry.project_id, entry.job_id, entry.expected_revision) == (None, None, None)
    assert (entry.progress.total_chapters, entry.progress.completed_chapters, entry.progress.failed_chapters, entry.progress.failed_segments) == (3, 1, 1, 2)
    assert seen.selection is None and seen.provider_calls_may_repeat
    assert tree(tmp_path) == before and path.exists()


def test_an_old_file_resumes_only_through_the_current_flow_and_stays_untouched(scene, tmp_path):
    path = _write(tmp_path, "book-pt.ayvu-state.json", _v1(tmp_path))
    before = path.read_bytes()
    runner = Runner(scene)

    result = scene.request(runner=runner)

    assert result.status is ResumeStatus.LEGACY_REEXECUTION and runner.legacy == [result.reference]
    assert not (tmp_path / "processing" / "projects").exists() and path.read_bytes() == before


def test_an_old_file_with_a_project_but_no_job_on_its_plan_stays_legacy(scene, tmp_path):
    scene.save_state()
    scene.project()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and entry.project_id is not None and entry.job_id is None


def test_a_changed_book_with_a_project_known_elsewhere_is_refused_on_the_legacy_path_too(tmp_path):
    scene = Scene(tmp_path)
    scene.project()
    _book(scene.source, chapter_two="<p>Now it is another book.</p>")
    other = Scene(tmp_path, source=scene.source, url="http://another.example.test")
    other.save_state()

    entry = other.only_entry()
    seen = other.recovery().inspect.execute(entry.reference)

    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.SOURCE_CHANGED in seen.entry.reasons and not seen.entry.resumable


@pytest.mark.parametrize("name, content, reason", [
    ("bad.ayvu-state.json", b"\xff\xfe{}", RecoveryReason.INVALID_STATE_FILE),
    ("json.ayvu-state.json", b"{not json", RecoveryReason.INVALID_STATE_FILE),
    ("list.ayvu-state.json", b"[]", RecoveryReason.INVALID_STATE_FILE),
    ("field.ayvu-state.json", b'{"version": 1}', RecoveryReason.INVALID_STATE_FILE),
    ("newer.ayvu-state.json", b'{"version": 2}', RecoveryReason.UNSUPPORTED_SCHEMA),
    ("huge.ayvu-state.json", b" " * (MAX_LEGACY_STATE_BYTES + 1), RecoveryReason.INVALID_STATE_FILE),
], ids=["not_utf8", "not_json", "not_an_object", "missing_field", "newer_version", "too_large"])
def test_a_bad_file_is_one_unsupported_entry_and_is_left_alone(scene, tmp_path, name, content, reason):
    path = _write(tmp_path, name, content)
    before = tree(tmp_path)
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.UNSUPPORTED and entry.reasons == (reason,) and not entry.resumable
    assert tree(tmp_path) == before and path.read_bytes() == content


def test_a_directory_or_link_by_that_name_is_not_read(scene, tmp_path):
    (tmp_path / "processing").mkdir()
    (tmp_path / "processing" / "dir.ayvu-state.json").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_v1(tmp_path)))
    (tmp_path / "processing" / "link.ayvu-state.json").symlink_to(target)

    entries = scene.recovery().list_interrupted.execute().entries

    assert [entry.compatibility for entry in entries] == [CompatibilityStatus.UNSUPPORTED] * 2


# ---------- What stays out of sight.

def test_nothing_identifying_reaches_a_result_an_event_or_a_log(tmp_path, caplog):
    canary = "Canary Book Title"
    folder = tmp_path / canary
    folder.mkdir()
    scene = Scene(tmp_path, source=_book(folder / f"{canary}.epub"), output=folder / f"{canary} out.epub")
    state_file = scene.interrupted_state()
    with caplog.at_level(logging.DEBUG):
        recovery = scene.recovery()
        listing = recovery.list_interrupted.execute()
        entry = listing.entries[0]
        seen = recovery.inspect.execute(entry.reference)
        result = scene.request(recovery=recovery)
    legacy = recovery.evidence.legacy_entry(entry.reference)
    text = " ".join(repr(item) for item in (listing, entry, seen, seen.entry, seen.selection, result, recovery, recovery.evidence, legacy,
                                            result.inspection, *result.result.events))
    text += " ".join(entry.reference for entry in listing.entries) + caplog.text
    for secret in (canary, str(folder), CANARY_URL, "canary.example", str(state_file), "PT:", "Hello reader"):
        assert secret not in text


def test_the_error_from_a_bad_file_quotes_nothing_from_it(tmp_path):
    from ayvu.legacy_resume import LegacyResumeError, inspect_legacy_resume
    path = _write(tmp_path, "x.ayvu-state.json", {"version": 1, "status": "Canary status"})
    with pytest.raises(LegacyResumeError) as caught:
        inspect_legacy_resume(path)
    assert "Canary" not in str(caught.value) and "x.ayvu" not in repr(caught.value)


def test_a_job_list_can_be_read_newest_first_and_ready_artifacts_listed(scene):
    _finished(scene)
    scene.save_state()
    scene.request()
    project = scene.project()
    repository = scene.jobs_repository()
    newest = repository.list_jobs(project.project_id, newest_first=True)
    oldest = repository.list_jobs(project.project_id)
    assert [item.job.job_id for item in newest] == [item.job.job_id for item in reversed(oldest)]
    assert newest[0].job.continuation_of == oldest[0].job.job_id
    paths = scene.projects.resolve_database_paths(project.project_id)
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT count(*) FROM job_artifacts WHERE state = 'ready'").fetchone() == (2,)


def test_a_link_at_the_destination_stays_a_link_while_the_plan_holds_the_real_path(tmp_path):
    real = tmp_path / "real.epub"
    real.write_bytes(b"old")
    link = tmp_path / "link.epub"
    link.symlink_to(real)
    scene = Scene(tmp_path, output=link)
    assert scene.translate(overwrite=True).result.state is JobState.VALIDATED
    assert link.is_symlink() and real.read_bytes() != b"old"


def test_a_guard_that_cannot_be_read_clears_nobody(scene, tmp_path):
    scene.interrupted_state()
    job = scene.jobs()[0]
    guard = scene.projects.run_guard(job.request.project_id)
    lock = guard._path(job.request.input_fingerprint)
    lock.unlink()
    (tmp_path / "elsewhere").write_text("x")
    lock.symlink_to(tmp_path / "elsewhere")
    entry = scene.only_entry()
    assert RecoveryReason.ACTIVE_LEASE in entry.reasons and not entry.resumable


def test_the_role_recovery_looks_for_is_the_one_the_rich_path_writes():
    from ayvu.job_recovery_store import RAW_RESULTS_ROLE as read
    from ayvu.translation_document import RAW_RESULTS_ROLE as written
    assert read == written

