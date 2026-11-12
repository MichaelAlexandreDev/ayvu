from __future__ import annotations
import dataclasses
import errno
import json
import logging
import os
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from ayvu.artifacts.model import ArtifactRequest, ArtifactState
from ayvu.artifacts.destination import DestinationFileOps, LocalDestinationPayloads
from ayvu.v1.cache import TranslationCache
from ayvu.v1.epub import translate_epub
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.jobs.model import JobState, JobStoreError
from ayvu.jobs.events import JobEventKind, encode_event
from ayvu.jobs.execution import CancellationSource
from ayvu.projects.schema import open_artifact_service, open_job_repository, open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.provenance.model import ProvenanceStoreError
from ayvu.translation.project import TranslateProject
from ayvu.translation.project_contracts import (OUTPUT_ROLE, RECONSTRUCT_STAGE,
    RECONSTRUCT_STAGE_VERSION, TRANSLATE_BEFORE_START, ContinuationTicket, OutputValidation, TranslateCommandError,
    TranslateObserver, TranslateProjectCommand, TranslateProjectPorts, resolve_execution_plan as resolve_plan)
from ayvu.composition.builders.translate_project import LegacyOutputValidator, LegacyResources, LegacyTranslationRuntime, open_translate_project
from ayvu.translation.engine import ENGINE_BEFORE_CHAPTER, ENGINE_BEFORE_UNIT, EngineObserver
from ayvu.providers.libretranslate.client import TranslatorError
from tests.support.epub_fakes import OPF, XHTML, epub_bytes
from ayvu.kernel import ChapterSelection, LanguagePair, OutputPlan, TranslationMemoryOptions, TranslationOptions, JobFailure, JobFailureFamily

PAIR = LanguagePair(source="en", target="pt")


# ebooklib, which the current validator opens every book with, rejects a remote manifest item.
OPENABLE_OPF = OPF.replace(b'    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>\n', b"")


OPF_MEMBER = "OEBPS/content.opf"


# The current validator calls a percent-encoded link broken, so a clean run needs a chapter without one.
CLEAN_CHAPTER = XHTML.format('<p>Hello <b>world</b></p><img src="../images/a.png" alt="A picture"/>').encode()


CANARY_TEXT = "Canary source sentence"


CANARY_TRANSLATION = "Canary translated sentence"


CANARY_URL = "http://canary.example.test:5000"


CANARY_GLOSSARY = "Canaryterm"


class Crash(BaseException):
    """Nothing in production catches this one."""


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Prefix:
    def __init__(self, change=None, on_call=None):
        self.change = change or (lambda text: f"PT:{text}")
        self.on_call = on_call
        self.calls = []
        self.lock = threading.Lock()

    def translate(self, text, source, target):
        with self.lock:
            self.calls.append(text)
        if self.on_call is not None:
            self.on_call(text)
        return self.change(text)


class BreakableOps(DestinationFileOps):
    """Runs a hook before or after one operation, and dies with the process if a hook says so."""

    def __init__(self):
        self.before, self.after, self.dead = {}, {}, False

    def _do(self, name, *args):
        if self.dead:
            raise Crash
        if name in self.before:
            self.before[name](*args)
        result = getattr(super(), name)(*args)
        if name in self.after:
            self.after[name](*args)
        return result

    def crash(self, *_):
        self.dead = True
        raise Crash

    def open_new(self, path):
        return self._do("open_new", path)

    def write(self, stream, data):
        return self._do("write", stream, data)

    def sync(self, stream):
        return self._do("sync", stream)

    def link(self, source, target):
        return self._do("link", source, target)

    def unlink(self, path):
        return self._do("unlink", path)

    def sync_directory(self, path):
        return self._do("sync_directory", path)

    def replace(self, source, target):
        return self._do("replace", source, target)

    def chmod(self, path, mode):
        return self._do("chmod", path, mode)

    def reserve_name(self, path, mode):
        return self._do("reserve_name", path, mode)


class CancelAt(CancellationSource):
    """Asks for cancellation exactly when one safe point is reached."""

    def __init__(self, safe_points, target):
        super().__init__(safe_points)
        inner, self.reached, self.target = self.token, False, target

        class Token:
            def checkpoint(_, safe_point):
                if safe_point == target:
                    self.reached = True
                    self.request("user_requested", datetime(2026, 11, 4, 12, tzinfo=UTC))
                return inner.checkpoint(safe_point)

        self.token = Token()


class World:
    def __init__(self, tmp_path, opf=OPENABLE_OPF, chapter=CLEAN_CHAPTER, second=None, raw=None):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.processing = tmp_path / "processing"
        self.source = tmp_path / "book.epub"
        replace = {OPF_MEMBER: opf, "OEBPS/text/one.xhtml": chapter}
        if second is not None:
            replace["OEBPS/text/two words.xhtml"] = second
        self.source.write_bytes(raw if raw is not None else epub_bytes(replace=replace))
        self.source_bytes = self.source.read_bytes()
        self.destination = tmp_path / "out" / "out.epub"
        self.cache = tmp_path / "cache.sqlite"
        self.ops = BreakableOps()
        request = build_project_request(self.source, source="en", target="pt",
                                        selections={"translator": "libretranslate", "url": CANARY_URL})
        self.projects = ProjectJsonRepository(self.processing)
        self.project = self.projects.resolve(request).project
        self.translator = Prefix()
        self.glossary = None
        self.factory_calls = {"translator": 0, "cache": 0}

    def cache_factory(self):
        self.factory_calls["cache"] += 1
        return TranslationCache(self.cache)

    def translator_factory(self):
        self.factory_calls["translator"] += 1
        return self.translator

    def prepare(self):
        return LegacyResources(self.translator, self.glossary, self.translator_factory)

    def runtime(self, options, prepare=None):
        return LegacyTranslationRuntime(self.source, options, prepare or self.prepare, self.cache_factory)

    def command(self, *, destination=None, overwrite=False, **options):
        return TranslateProjectCommand(self.project.project_id, self.project.source_sha256,
                                       TranslationOptions(language_pair=PAIR, **options),
                                       OutputPlan(path=destination or self.destination), overwrite=overwrite)

    def use_case(self, command, prepare=None, *, destination=None, **tuning):
        tuning.setdefault("lease_renewal", timedelta(hours=1))
        self.runtime_used = self.runtime(command.options, prepare)
        return open_translate_project(self.processing, self.project.project_id, self.source, destination or command.output.path,
                                      overwrite=command.overwrite, runtime=self.runtime_used, clock=self.clock, ops=self.ops, **tuning)

    def run(self, *, destination=None, overwrite=False, prepare=None, cancellation=None, observer=None, **options):
        command = self.command(destination=destination, overwrite=overwrite, **options)
        return self.use_case(command, prepare).execute(command, cancellation=cancellation, observer=observer)

    def _paths(self):
        return self.projects.resolve_database_paths(self.project.project_id)

    def jobs(self):
        paths = self._paths()
        return open_job_repository(paths.database, paths.backups, self.project.project_id, clock=self.clock)

    def all_jobs(self):
        return [item.job for item in self.jobs().list_jobs(self.project.project_id)]

    def provenance(self):
        paths = self._paths()
        return open_provenance_repository(paths.database, paths.backups, self.project.project_id, clock=self.clock)

    def records(self, job_id):
        return [node.record.request for node in self.provenance().list_by_job(job_id, limit=64).nodes]

    def artifacts_of(self, job):
        paths = self._paths()
        with sqlite3.connect(paths.database) as connection:
            return [row for row in connection.execute("SELECT state, reason_code FROM job_artifacts WHERE job_id = ?", (job.job_id,))]

    def recover(self, command=None):
        """A new process looks at the project."""
        self.ops.dead = False
        self.clock.advance(hours=2)
        command = command or self.command()
        return self.use_case(command).reconcile(self.project.project_id)

    def leftovers(self):
        found = [path.name for path in self.destination.parent.iterdir() if path.name.endswith(".part")] if self.destination.parent.exists() else []
        journals = self.projects.resolve_artifact_root(self.project.project_id) / "publications"
        return found + ([path.name for path in journals.iterdir()] if journals.exists() else [])

    def assert_consistent(self, result):
        """The one terminal view: the record, the result, the events and the lineage say the same thing."""
        job = self.jobs().get(result.job_id).job
        assert job.state is result.state and job.terminal and job.lease is None
        assert result.outcome.matches(job)
        events = result.events
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[-1].payload == result.outcome and [e.kind for e in events].count(JobEventKind.OUTCOME) == 1
        records = self.records(result.job_id)
        terminal = [record for record in records if record.operation == "translate_project"
                    and record.outcome_code == job.terminal_summary.reason_code]
        assert len(terminal) == 1 and not terminal[0].producer.fully_identified
        expected = () if result.artifact is None else (result.artifact.artifact_id,)
        assert terminal[0].artifacts == expected
        if result.state in (JobState.FAILED, JobState.CANCELLED):
            assert result.artifact is None and not result.output_ready
        assert self.source.read_bytes() == self.source_bytes


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def current_flow(world, directory, **options):
    directory.mkdir()
    translator = Prefix(world.translator.change)
    with TranslationCache(directory / "cache.sqlite") as cache:
        factories = {}
        if options.get("workers", 1) > 1:
            factories = dict(translator_factory=lambda: translator, cache_factory=lambda: TranslationCache(directory / "cache.sqlite"))
        report = translate_epub(world.source, directory / "out.epub", translator, cache,
                                TranslationOptions(language_pair=PAIR, **options), **factories)
    return report, (directory / "out.epub").read_bytes() if report.output_written else None, translator


SCENARIOS = {
    "plain": {},
    "metadata-and-alt": dict(translate_metadata=True, translate_alt_text=True),
    "selection": dict(chapter_selection=ChapterSelection.parse("2")),
    "small-chunks": dict(chunk_limit=20),
    "cache-only": dict(cache_only=True),
    "workers": dict(workers=2),
}


CANCEL_POINTS = [TRANSLATE_BEFORE_START, "format_before_work", "epub_between_members", ENGINE_BEFORE_CHAPTER, ENGINE_BEFORE_UNIT,
                 "artifact_before_write", "artifact_between_chunks", "artifact_before_publication"]


def declared_points(world):
    command = world.command()
    return world.use_case(command).cancellation()._safe_points


def _job_request(world, command):
    from ayvu.jobs.model import JobRequest
    return JobRequest(world.project.project_id, "translate_project", 1, resolve_plan(command, world.project).input_fingerprint, (OUTPUT_ROLE,))


def _job_left_by_a_dead_process(world, command, *, state=JobState.RUNNING, fingerprint=None):
    jobs = world.jobs()
    request = _job_request(world, command)
    if fingerprint is not None:
        request = replace(request, input_fingerprint=fingerprint)
    job = jobs.create(request)
    job = jobs.acquire_lease(job.job_id, timedelta(hours=1))
    if state is not JobState.QUEUED:
        job = jobs.transition(job.job_id, JobState.PREFLIGHT, expected_revision=job.revision)
        job = jobs.transition(job.job_id, JobState.RUNNING, expected_revision=job.revision)
    return job
