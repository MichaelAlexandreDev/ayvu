from __future__ import annotations
import hashlib
import json
import logging
import socket
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from typer.testing import CliRunner
from ayvu.knowledge.glossary import load_glossary
from ayvu.interfaces.cli.output import EXISTING_OUTPUT_OVERWRITE_OPTION
from ayvu.interfaces.cli.app import app
from ayvu.preferences.config_facade import AyvuConfig, ConfigStore, TranslationProfile, default_config_path
from ayvu.jobs.model import JobRequest, JobState
from ayvu.recovery.service import CompatibilityStatus, RequiredDecision, ResumeRequest, ResumeStatus
from ayvu.translation.resume_reuse import RecoveryReason, ResumeStage
from ayvu.recovery.checkpoint_scan import MAX_LEGACY_STATE_BYTES, scan_legacy_states
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_job_repository
from ayvu.projects.store import ProjectJsonRepository, build_project_request, translation_selections
from ayvu.recovery.checkpoint import ResumeStateStore, TranslationResumeState
from ayvu.translation.project_contracts import (
    OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, ContinuationTicket, TranslateObserver, TranslateProjectCommand,
)
from ayvu.composition.builders.translate_project import LegacyResources, open_job_recovery, open_translate_project
from tests.support.engine_runtime_support import EngineTestRuntime
from tests.support.legacy_cache_file import LegacyCacheFile
from tests.support.cli_translate_project_support import ENCRYPTED_DOCUMENT, Session, _args, _book
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.kernel.sqlite import SqliteMigrationRunner

PAIR = LanguagePair(source="en", target="pt")


CANARY_URL = "http://canary.example.test:5000"


DECIDED = (RequiredDecision.FULL_REEXECUTION, RequiredDecision.REPLACE_DESTINATION)


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.interfaces.cli.settings._processing_dir", lambda _config: tmp_path / "processing")


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

    def __init__(self, tmp_path: Path, *, source=None, output=None, profile=None, glossary=None, url=CANARY_URL) -> None:
        self.tmp_path = tmp_path
        self.processing = tmp_path / "processing"
        self.source = source or _book(tmp_path / "book.epub")
        self.output = output or tmp_path / "out.epub"
        self.cache = tmp_path / "cache.sqlite"
        self.clock = Clock()
        self.translator = Prefix()
        self.options = TranslationOptions(language_pair=PAIR)
        self.profile = profile
        self.glossary = glossary
        self.url = url
        self.projects = ProjectJsonRepository(self.processing)

    def selections(self):
        return translation_selections(translator="libretranslate", url=self.url, chunk_limit=self.options.chunk_limit,
                                      translate_metadata=False, translate_alt_text=False, chapters=None, translation_memory=None)

    def project(self):
        return self.projects.resolve(build_project_request(self.source, source="en", target="pt", selections=self.selections(), profile_name=self.profile, glossary_path=self.glossary)).project

    def save_state(self, **changes) -> Path:
        state = TranslationResumeState.create(self.source, self.output, self.cache, "libretranslate", self.url, self.glossary, self.options, False, 30.0, 0,
                                              profile_name=self.profile)
        return ResumeStateStore(self.processing).save(replace(state, **changes))

    def runtime(self, translator=None):
        translator = translator or self.translator
        return EngineTestRuntime(self.source, self.options, lambda: LegacyResources(translator, load_glossary(self.glossary) if self.glossary else None),
                                 lambda: LegacyCacheFile(self.cache))

    def translate(self, *, overwrite=False, continuation=None, operation_id=None):
        """A run of the book's project, as the CLI starts one: the plan holds the canonical output path."""
        return run_in_project(self.processing, self.project(), self.source, self.output, self.options, self.runtime(),
                              overwrite=overwrite, continuation=continuation, operation_id=operation_id)

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


@dataclass(frozen=True)
class Run:
    result: object


def run_in_project(processing, project, source, output, options, runtime, *, overwrite=False, continuation=None, operation_id=None):
    use_case = open_translate_project(processing, project.project_id, source, output, overwrite=overwrite, runtime=runtime)
    command = TranslateProjectCommand(project.project_id, project.source_sha256, options, OutputPlan(path=output.expanduser().resolve()),
                                      overwrite=overwrite, operation_id=operation_id)
    return Run(use_case.execute(command, observer=TranslateObserver(), continuation=continuation))


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


# ---------- Finding the job from the file.

def _running_state(tmp_path):
    return next(item for item in scan_legacy_states(tmp_path / "processing"))


def _interrupt_real_cli(tmp_path, monkeypatch, arguments, stdin=None):
    from tests.support.cli_rich_runtime_support import config, declare_model
    path = default_config_path()
    if path.exists():
        declare_model(path)
    else:
        config(tmp_path)
    def stop(text):
        if "Goodbye" in text:
            raise KeyboardInterrupt
    session = Session(on_post=stop)
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    result = CliRunner().invoke(app, arguments, input=stdin)
    assert result.exit_code == 1, result.output
    return open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]


# ---------- The output.

def _finished(scene):
    scene.save_state()
    assert scene.translate().result.state is JobState.VALIDATED


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
