"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
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

from ayvu.artifact import ArtifactRequest, ArtifactState
from ayvu.artifact_destination import DestinationFileOps, LocalDestinationPayloads
from ayvu.cache import TranslationCache
from ayvu.domain import ChapterSelection, LanguagePair, OutputPlan, TranslationMemoryOptions, TranslationOptions
from ayvu.epub_io import translate_epub
from ayvu.glossary import load_glossary
from ayvu.job import JobState, JobStoreError
from ayvu.job_events import JobEventKind, JobFailure, JobFailureFamily, encode_event
from ayvu.job_execution import CancellationSource
from ayvu.project_schema import open_artifact_service, open_job_repository, open_provenance_repository
from ayvu.project_store import ProjectJsonRepository, build_project_request
from ayvu.provenance import ProvenanceStoreError
from ayvu.translate_project import (
    OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, TRANSLATE_BEFORE_START, OutputValidation, TranslateCommandError,
    TranslateObserver, TranslateProject, TranslateProjectCommand, TranslateProjectPorts, resolve_plan,
)
from ayvu.translate_project_composition import (
    LegacyOutputValidator, LegacyResources, LegacyTranslationRuntime, open_translate_project,
)
from ayvu.translation_engine import ENGINE_BEFORE_CHAPTER, ENGINE_BEFORE_UNIT, EngineObserver
from ayvu.translator import TranslatorError
from epub_fakes import OPF, XHTML, epub_bytes

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
        assert len(records) == 1 and records[0].outcome_code == job.terminal_summary.reason_code
        assert not records[0].producer.fully_identified
        expected = () if result.artifact is None else (result.artifact.artifact_id,)
        assert records[0].artifacts == expected
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


def test_a_clean_run_publishes_the_output_and_ends_validated(world):
    seen = []
    result = world.run(observer=TranslateObserver(on_event=seen.append))
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and result.output_ready and result.failure is None
    assert result.artifact.state is ArtifactState.READY and world.destination.exists()
    assert seen == list(result.events)
    assert result.validation.ok and result.validation.document_count > 0
    assert world.leftovers() == []
    phases = [event.payload.phase for event in result.events if event.kind is JobEventKind.PROGRESS]
    assert list(dict.fromkeys(phases)) == ["preflight", "translating", "validating", "publishing"]


def test_the_output_has_the_mode_a_new_file_gets_and_the_temporary_name_says_nothing_about_the_book(world):
    names = []
    world.ops.before["open_new"] = names.append
    old = os.umask(0o022)
    try:
        result = world.run()
    finally:
        os.umask(old)
    assert result.state is JobState.VALIDATED
    assert (world.destination.stat().st_mode & 0o777) == 0o644
    assert len(names) == 1 and names[0].parent == world.destination.parent and "out" not in names[0].name and "book" not in names[0].name


SCENARIOS = {
    "plain": {},
    "metadata-and-alt": dict(translate_metadata=True, translate_alt_text=True),
    "selection": dict(chapter_selection=ChapterSelection.parse("2")),
    "small-chunks": dict(chunk_limit=20),
    "cache-only": dict(cache_only=True),
    "workers": dict(workers=2),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_output_and_the_report_match_the_current_flow(name, tmp_path):
    world = World(tmp_path)
    options = SCENARIOS[name]
    report, output, translator = current_flow(world, tmp_path / "current", **options)
    result = world.run(**options)
    world.assert_consistent(result)
    # Workers sharing a cache can race on a repeated text, so only the set of calls is stable.
    compared = set if options.get("workers") else list
    assert compared(world.translator.calls) == compared(translator.calls)
    assert world.destination.read_bytes() == output
    produced = world.runtime_used.report(result, world.destination)
    for field in ("texts_missing", "alt_texts_translated", "errors", "chapters_processed", "output_written", "detected_language", "target_language"):
        assert getattr(produced, field) == getattr(report, field), field
    # The same race decides which of the two counts a repeated text lands in.
    assert produced.texts_translated + produced.texts_from_cache == report.texts_translated + report.texts_from_cache
    if not options.get("workers"):
        assert produced.texts_from_cache == report.texts_from_cache


def test_missing_texts_keep_the_output_and_end_partial(world):
    result = world.run(cache_only=True)
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "texts_missing"
    assert world.destination.exists() and world.translator.calls == []


def test_translation_memory_applies_through_the_use_case(tmp_path):
    world = World(tmp_path, chapter=XHTML.format("<p>The quick brown fox jumps over the lazy dog and runs</p>").encode(),
                  second=XHTML.format("<p>The quick brown fox jumps over the lazy dog and ran</p>").encode())
    world.run(chapter_selection=ChapterSelection.parse("1"))
    world.destination.unlink()
    memory = TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.5)
    result = world.run(translation_memory=memory)
    assert result.engine.stats.from_memory == 1


def test_a_failed_text_keeps_the_output_and_ends_partial(world):
    def change(text):
        if "Second" in text:
            raise TranslatorError("translator went away")
        return f"PT:{text}"
    world.translator.change = change
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "texts_failed"
    assert result.output_ready and world.destination.exists()
    assert dict(result.outcome.summary.counters)["errors"] == 1


def test_validation_warnings_keep_the_output_and_end_partial(tmp_path):
    world = World(tmp_path, chapter=XHTML.format('<p>Hello <a href="missing.xhtml">gone</a></p>').encode())
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "validation_warnings"
    assert world.destination.exists() and result.output_ready
    assert any("missing.xhtml" in warning for warning in result.validation.warnings)
    warnings = [event.payload for event in result.events if event.kind is JobEventKind.WARNING]
    assert [warning.code for warning in warnings] == ["validation_warnings"]


def test_a_file_damaged_as_it_is_published_is_not_called_ready(world):
    def damage(source, target):
        with open(target, "r+b") as stream:
            stream.truncate(10)
    world.ops.after["link"] = damage
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "final_payload_mismatch"
    assert world.artifacts_of(world.all_jobs()[0]) == [("abandoned", "final_mismatch")]


def test_the_validator_reads_the_bytes_before_the_destination_changes(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    seen = []
    def on_progress(index, total, name):
        seen.append(world.destination.read_bytes())
    result = world.run(overwrite=True, observer=TranslateObserver(on_validation_progress=on_progress))
    assert result.state is JobState.VALIDATED and seen and set(seen) == {b"the old book"}
    assert world.destination.read_bytes() != b"the old book"


def test_a_validator_that_breaks_cannot_end_the_job_as_validated(world):
    command = world.command()
    use_case = world.use_case(command)
    class Broken:
        def validate(self, staged, on_progress):
            raise RuntimeError("quoting the book")
    use_case._ports = dataclasses.replace(use_case._ports, validator=Broken())
    result = use_case.execute(command)
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and world.destination.exists()
    assert [event.payload.code for event in result.events if event.kind is JobEventKind.WARNING] == ["validation_unavailable"]


def test_a_warning_never_names_the_temporary_file(tmp_path):
    validator = LegacyOutputValidator(tmp_path / "out.epub")
    staged = tmp_path / ".ayvu-x.part"
    result = validator.validate(staged, None)
    assert not result.ok and str(staged) not in result.warnings[0] and str(tmp_path / "out.epub") in result.warnings[0]


def test_a_required_cache_with_gaps_publishes_nothing(world):
    result = world.run(cache_only=True, require_full_cache=True)
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "cache_incomplete"
    assert not world.destination.exists() and result.artifact is None and not result.output_ready
    assert world.translator.calls == []
    assert world.artifacts_of(world.all_jobs()[0]) == []


def test_fail_fast_ends_failed_with_nothing_published_and_the_exception_kept_for_the_presentation(world):
    def change(text):
        raise TranslatorError(f"could not translate {text}")
    world.translator.change = change
    result = world.run(fail_fast=True)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and not world.destination.exists()
    assert result.failure.family is JobFailureFamily.PROVIDER_TRANSIENT and result.failure.code == "provider_failed"
    assert isinstance(result.raised, TranslatorError)
    assert "could not" not in repr(result)
    assert world.leftovers() == []


def test_a_preflight_failure_ends_failed_before_the_book_is_read(world):
    def prepare():
        raise ConnectionError("the translator at http://secret.example is down")
    result = world.run(prepare=prepare)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "preflight_failed"
    assert isinstance(world.runtime_used.preflight_error, ConnectionError) and world.translator.calls == []
    assert not world.destination.exists()
    assert "secret" not in "".join(encode_event(event) for event in result.events)


def test_workers_without_a_factory_stop_in_preflight(world):
    result = world.run(workers=2, prepare=lambda: LegacyResources(world.translator))
    assert result.state is JobState.FAILED and result.failure.code == "preflight_failed"
    assert world.translator.calls == []


def test_no_chapter_matching_ends_failed_as_an_input_problem(world):
    result = world.run(chapter_selection=ChapterSelection.parse("nothing-like-this"))
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.family is JobFailureFamily.INPUT
    assert result.failure.code == "no_chapters_matched" and world.translator.calls == []


def test_a_book_the_adapter_refuses_ends_failed_without_a_provider_call(tmp_path):
    world = World(tmp_path, raw=b"this is not a zip file at all")
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.family is not JobFailureFamily.INTERNAL
    assert world.translator.calls == [] and not world.destination.exists()


def test_a_source_changed_during_the_run_publishes_nothing(world):
    def change_the_book(text):
        world.source.write_bytes(world.source_bytes + b"changed")
    world.translator.on_call = change_the_book
    result = world.run()
    assert result.state is JobState.FAILED and result.failure.code == "source_changed"
    assert result.failure.family is JobFailureFamily.INTEGRITY
    assert not world.destination.exists() and world.leftovers() == []


@pytest.mark.parametrize("kwargs, code", [
    (dict(project_id="00000000-0000-4000-8000-000000000999"), "manifest_missing"),
    (dict(source_sha256="0" * 64), "source_changed"),
    (dict(options=TranslationOptions(language_pair=LanguagePair("en", "es"))), "language_pair_mismatch"),
    (dict(adapter_id="no_such_format"), "unknown_adapter"),
])
def test_a_command_that_does_not_fit_the_project_creates_no_job(world, kwargs, code):
    command = dataclasses.replace(world.command(), **kwargs)
    result = world.use_case(world.command()).execute(command)
    assert result.job_id is None and result.state is None and result.failure.code == code and result.events == ()
    assert world.all_jobs() == [] and world.translator.calls == []


def test_a_source_that_no_longer_matches_its_revision_creates_no_job(world):
    world.source.write_bytes(world.source_bytes + b"changed")
    result = world.run()
    assert result.job_id is None and result.failure.code == "source_changed"
    assert world.all_jobs() == [] and world.translator.calls == []


def test_an_unusable_job_store_stops_before_any_provider_call(world):
    command = world.command()
    use_case = world.use_case(command)
    class Busy:
        def list_jobs(self, *a, **k):
            raise JobStoreError("busy")
    use_case._ports = dataclasses.replace(use_case._ports, jobs=Busy())
    result = use_case.execute(command)
    assert result.job_id is None and result.failure.family is JobFailureFamily.RESOURCE and result.failure.code == "busy"
    assert world.translator.calls == [] and not world.destination.exists()


@pytest.mark.parametrize("options, code", [
    (dict(dry_run=True), "dry_run_needs_no_project"),
    (dict(max_documents=1), "preview_needs_no_project"),
    (dict(workers=0), "invalid_workers"),
])
def test_what_needs_no_project_is_refused_as_a_command(world, options, code):
    with pytest.raises(TranslateCommandError) as caught:
        world.command(**options)
    assert str(caught.value) == code


@pytest.mark.parametrize("change, code", [
    (dict(project_id="not-a-uuid"), "invalid_identity"),
    (dict(operation_id="not-a-uuid"), "invalid_identity"),
    (dict(source_sha256="abc"), "invalid_source_revision"),
    (dict(schema_version=2), "unsupported_command_version"),
    (dict(options=None), "invalid_command"),
    (dict(output=OutputPlan(path=Path("x"), dry_run=True)), "dry_run_needs_no_project"),
])
def test_a_malformed_command_is_refused(world, change, code):
    with pytest.raises(TranslateCommandError) as caught:
        dataclasses.replace(world.command(), **change)
    assert str(caught.value) == code


def test_the_plan_changes_with_anything_that_decides_the_result(world):
    base = world.command()
    fingerprint = resolve_plan(base, world.project).input_fingerprint
    assert resolve_plan(world.command(), world.project).input_fingerprint == fingerprint
    variants = [world.command(translate_metadata=True), world.command(translate_alt_text=True), world.command(chunk_limit=10),
                world.command(fail_fast=True), world.command(workers=2), world.command(cache_only=True),
                world.command(chapter_selection=ChapterSelection.parse("1")), world.command(overwrite=True),
                world.command(destination=world.tmp_path / "other.epub")]
    seen = {resolve_plan(item, world.project).input_fingerprint for item in variants}
    assert len(seen) == len(variants) and fingerprint not in seen
    assert resolve_plan(base, dataclasses.replace(world.project, glossary_ref="glossary:" + "a" * 64)).input_fingerprint != fingerprint
    assert resolve_plan(base, dataclasses.replace(world.project, configuration_ref="configuration:" + "b" * 64)).input_fingerprint != fingerprint
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolve_plan(base, world.project).input_fingerprint = "x"


def test_an_output_that_appeared_meanwhile_is_not_overwritten(world):
    def someone_else_writes(text):
        world.destination.parent.mkdir(exist_ok=True)
        world.destination.write_bytes(b"not ours")
    world.translator.on_call = someone_else_writes
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "output_exists"
    assert world.destination.read_bytes() == b"not ours" and world.leftovers() == []
    assert world.artifacts_of(world.all_jobs()[0]) == [("abandoned", "final_mismatch")]


def test_overwrite_replaces_the_file_and_keeps_its_mode(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.destination.chmod(0o640)
    result = world.run(overwrite=True)
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and world.destination.read_bytes() != b"the old book"
    assert (world.destination.stat().st_mode & 0o777) == 0o640 and world.leftovers() == []


def test_a_link_at_the_destination_stays_a_link(world):
    real = world.tmp_path / "real.epub"
    real.write_bytes(b"old")
    world.destination.parent.mkdir()
    world.destination.symlink_to(real)
    result = world.run(overwrite=True)
    assert result.state is JobState.VALIDATED
    assert world.destination.is_symlink() and real.read_bytes() != b"old"


def test_a_destination_that_is_the_source_is_refused(world):
    result = world.run(destination=world.source, overwrite=True)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "output_is_source"


def test_a_link_to_the_source_is_refused(world):
    link = world.tmp_path / "alias.epub"
    link.symlink_to(world.source)
    result = world.run(destination=link, overwrite=True)
    world.assert_consistent(result)
    assert result.failure.code == "output_is_source"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_a_filesystem_without_hard_links_still_publishes(world, code):
    def no_links(*_):
        raise OSError(code, "no hard links here")
    world.ops.before["link"] = no_links
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and world.leftovers() == []
    assert world.destination.stat().st_nlink == 1


def test_without_hard_links_an_output_that_appeared_meanwhile_is_still_not_overwritten(world):
    def no_links(*_):
        world.destination.parent.mkdir(exist_ok=True)
        world.destination.write_bytes(b"not ours")
        raise OSError(errno.EPERM, "no hard links here")
    world.ops.before["link"] = no_links
    result = world.run()
    assert result.failure.code == "output_exists" and world.destination.read_bytes() == b"not ours"
    assert world.leftovers() == []


def test_a_real_link_failure_is_reported_and_not_worked_around(world):
    def broken(*_):
        raise OSError(errno.EIO, "disk trouble")
    world.ops.before["link"] = broken
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "io" and not world.destination.exists()


def test_a_full_disk_while_writing_leaves_nothing_behind(world):
    def full(stream, data):
        raise OSError(errno.ENOSPC, "no space")
    world.ops.before["write"] = full
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and not world.destination.exists() and world.leftovers() == []


CANCEL_POINTS = [TRANSLATE_BEFORE_START, "format_before_work", "epub_between_members", ENGINE_BEFORE_CHAPTER, ENGINE_BEFORE_UNIT,
                 "artifact_before_write", "artifact_between_chunks", "artifact_before_publication"]


def declared_points(world):
    command = world.command()
    return world.use_case(command).cancellation()._safe_points


def test_the_cancel_points_under_test_are_real(world):
    known = declared_points(world)
    for point in CANCEL_POINTS:
        if point != "epub_between_members":
            assert point in known
    assert any("member" in point for point in known)


@pytest.mark.parametrize("point", CANCEL_POINTS)
def test_cancelling_at_a_safe_point_ends_cancelled_with_nothing_published(world, point):
    command = world.command()
    use_case = world.use_case(command)
    if point == "epub_between_members":
        point = next(item for item in use_case.cancellation()._safe_points if "member" in item)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), point)
    result = use_case.execute(command, cancellation=source)
    assert source.reached
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and not world.destination.exists() and world.leftovers() == []
    assert result.engine.stats.errors == []
    kinds = [event.kind for event in result.events]
    assert kinds[-3:] == [JobEventKind.CANCELLATION_REQUESTED, JobEventKind.CANCELLATION_ACKNOWLEDGED, JobEventKind.OUTCOME]
    assert source.outcome is result.outcome


def test_cancelling_keeps_the_old_file_under_overwrite(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    command = world.command(overwrite=True)
    use_case = world.use_case(command)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), "artifact_before_publication")
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and world.destination.read_bytes() == b"the old book" and world.leftovers() == []


def test_cancelling_before_the_start_never_reaches_the_translator_or_the_preflight(world):
    called = []
    command = world.command()
    use_case = world.use_case(command, lambda: called.append(1))
    source = CancelAt(tuple(use_case.cancellation()._safe_points), TRANSLATE_BEFORE_START)
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and called == [] and world.translator.calls == []


def test_cancelling_stops_the_workers_and_the_chapters_still_queued(world):
    command = world.command(workers=2)
    use_case = world.use_case(command)
    source = use_case.cancellation()
    world.translator.on_call = lambda text: source.request("user_requested", world.clock())
    result = use_case.execute(command, cancellation=source)
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and not world.destination.exists() and world.leftovers() == []
    assert len(world.translator.calls) <= 2 and result.engine.stats.errors == []


def test_cancelling_before_the_book_is_read_never_reaches_the_translator(world):
    command = world.command()
    use_case = world.use_case(command)
    source = CancelAt(tuple(use_case.cancellation()._safe_points), "format_before_work")
    result = use_case.execute(command, cancellation=source)
    assert result.state is JobState.CANCELLED and world.translator.calls == []


def test_a_failure_after_a_worker_committed_to_cancelling_still_ends_cancelled(tmp_path):
    # A unit after "Second", so that its worker reaches a safe point once the request is in.
    world = World(tmp_path, second=XHTML.format("<p>Second</p><p>Third</p>").encode())
    command = world.command(workers=2, fail_fast=True)
    use_case = world.use_case(command)
    source = use_case.cancellation()
    committed = threading.Event()
    inner = source.token

    class Token:
        def checkpoint(self, safe_point):
            answer = inner.checkpoint(safe_point)
            if answer is not None:
                committed.set()
            return answer

    source.token = Token()

    first_is_inside = threading.Event()

    def on_call(text):
        if "Second" in text:
            assert first_is_inside.wait(timeout=30)
            source.request("user_requested", world.clock())
        elif "Hello" in text:
            first_is_inside.set()
            assert committed.wait(timeout=30)
            raise TranslatorError("the first chapter fails only now")
    world.translator.on_call = on_call
    result = use_case.execute(command, cancellation=source)
    world.assert_consistent(result)
    assert result.state is JobState.CANCELLED and result.recorded


def test_ctrl_c_stops_the_chapter_another_worker_is_in(tmp_path, monkeypatch):
    released = threading.Event()

    class SpyEvent(threading.Event):
        def set(self):
            super().set()
            released.set()

    monkeypatch.setattr("ayvu.translation_engine.Event", SpyEvent)
    world = World(tmp_path, second=XHTML.format("<p>Alpha</p><p>Beta</p><p>Gamma</p>").encode())

    def on_call(text):
        if "Alpha" in text:
            assert released.wait(timeout=30)
        elif "Hello" in text:
            raise KeyboardInterrupt
    world.translator.on_call = on_call
    with pytest.raises(KeyboardInterrupt):
        world.run(workers=2)
    assert not any("Beta" in text or "Gamma" in text for text in world.translator.calls)
    assert world.all_jobs()[0].state is JobState.CANCELLED


def test_a_cancellation_that_arrives_after_the_last_safe_point_is_too_late_and_says_so(world):
    command = world.command()
    use_case = world.use_case(command)
    source = use_case.cancellation()
    answers = []
    def on_event(event):
        payload = event.payload
        if getattr(payload, "phase", None) == "publishing" and payload.completed == 1:
            answers.append(source.request("user_requested", world.clock()).status.value)
    result = use_case.execute(command, cancellation=source, observer=TranslateObserver(on_event=on_event))
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and answers == ["accepted"]
    assert source.request("again", world.clock()).status.value == "too_late"


def test_a_consumer_that_fails_cannot_change_how_the_job_ends(world):
    def on_event(event):
        raise RuntimeError("the window was closed")
    result = world.run(observer=TranslateObserver(on_event=on_event))
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED


def test_an_engine_observer_that_fails_ends_the_job_as_a_failure(world):
    def on_chapter_done(*_):
        raise RuntimeError("could not save the resume file")
    result = world.run(observer=TranslateObserver(engine=EngineObserver(on_chapter_done=on_chapter_done)))
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and isinstance(result.raised, RuntimeError)


@pytest.mark.parametrize("workers", [1, 2])
def test_ctrl_c_during_translation_ends_cancelled_and_reaches_the_caller(world, workers):
    def interrupt(text):
        raise KeyboardInterrupt
    world.translator.on_call = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(workers=workers)
    job = world.all_jobs()[0]
    assert job.state is JobState.CANCELLED and job.lease is None
    assert not world.destination.exists() and world.leftovers() == []


def test_ctrl_c_during_the_preflight_ends_failed(world):
    def prepare():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(prepare=prepare)
    job = world.all_jobs()[0]
    assert job.state is JobState.FAILED and job.terminal_summary.reason_code == "interrupted"


def test_ctrl_c_after_the_file_became_visible_says_the_file_is_there(world):
    def interrupt(*_):
        raise KeyboardInterrupt
    world.ops.after["link"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run()
    job = world.all_jobs()[0]
    assert job.state is JobState.PARTIALLY_COMPLETED and job.terminal_summary.reason_code == "interrupted_after_publication"
    assert world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(job) == [("ready", None)]


def test_ctrl_c_before_the_file_became_visible_leaves_the_old_file_alone(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    def interrupt(*_):
        raise KeyboardInterrupt
    world.ops.before["replace"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    assert job.state is JobState.CANCELLED
    assert world.destination.read_bytes() == b"the old book" and world.leftovers() == []


def test_a_crash_while_writing_is_settled_as_failed_and_the_temporary_file_goes(world):
    world.ops.after["write"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    assert [path.name for path in world.destination.parent.iterdir() if path.name.endswith(".part")]
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING
    assert world.recover() == (job.job_id,)
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.FAILED and job.terminal_summary.reason_code == "interrupted"
    assert not world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(job) == [("abandoned", "interrupted_before_digest")]


def test_a_crash_after_staging_never_touches_the_old_file(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.ops.before["replace"] = world.ops.crash
    with pytest.raises(Crash):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    world.recover(world.command(overwrite=True))
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.FAILED and world.destination.read_bytes() == b"the old book" and world.leftovers() == []
    assert world.artifacts_of(job) == [("abandoned", "final_mismatch")]


def test_a_crash_right_after_the_link_finishes_the_publication_on_recovery(world):
    world.ops.after["link"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    assert world.destination.exists() and world.destination.stat().st_nlink == 2
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING and world.artifacts_of(job) == [("staging", None)]
    assert world.recover() == (job.job_id,)
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and job.terminal_summary.reason_code == "interrupted_after_publication"
    assert world.artifacts_of(job) == [("ready", None)]
    assert world.destination.stat().st_nlink == 1 and world.leftovers() == []
    records = world.records(job.job_id)
    assert len(records) == 1 and len(records[0].artifacts) == 1 and records[0].outcome_code == "interrupted_after_publication"


def test_a_crash_right_after_the_swap_finishes_the_publication_on_recovery(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.ops.after["replace"] = world.ops.crash
    with pytest.raises(Crash):
        world.run(overwrite=True)
    job = world.all_jobs()[0]
    world.recover(world.command(overwrite=True))
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and world.artifacts_of(job) == [("ready", None)]
    assert world.destination.read_bytes() != b"the old book" and world.leftovers() == []


def test_a_crash_after_the_record_went_ready_still_ends_the_job(world):
    command = world.command()
    use_case = world.use_case(command)
    original = use_case._ports.jobs.transition
    def transition(job_id, target, **kwargs):
        if target is JobState.COMPLETED:
            raise Crash
        return original(job_id, target, **kwargs)
    use_case._ports.jobs.transition = transition
    with pytest.raises(Crash):
        use_case.execute(command)
    job = world.all_jobs()[0]
    assert job.state is JobState.RUNNING and world.artifacts_of(job) == [("ready", None)]
    world.recover()
    job = world.jobs().get(job.job_id).job
    assert job.state is JobState.PARTIALLY_COMPLETED and world.leftovers() == []


@pytest.mark.parametrize("stop_at, expected, reason", [
    (JobState.QUEUED, JobState.CANCELLED, "interrupted"),
    (JobState.PREFLIGHT, JobState.FAILED, "interrupted"),
    (JobState.RUNNING, JobState.FAILED, "interrupted"),
])
def test_a_job_left_behind_in_each_early_state_gets_one_terminal_outcome(world, stop_at, expected, reason):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    job = jobs.create(_job_request(world, command))
    job = jobs.acquire_lease(job.job_id, timedelta(minutes=30))
    for state in {JobState.QUEUED: (), JobState.PREFLIGHT: (JobState.PREFLIGHT,), JobState.RUNNING: (JobState.PREFLIGHT, JobState.RUNNING)}[stop_at]:
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    assert world.recover() == (job.job_id,)
    settled = world.jobs().get(job.job_id).job
    assert settled.state is expected and settled.terminal_summary.reason_code == reason
    assert world.recover() == ()


@pytest.mark.parametrize("steps, expected", [
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.CANCELLING), JobState.CANCELLED),
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED), JobState.PARTIALLY_COMPLETED),
    ((JobState.PREFLIGHT, JobState.RUNNING, JobState.COMPLETED, JobState.VALIDATING), JobState.PARTIALLY_COMPLETED),
])
def test_a_job_left_behind_in_each_late_state_gets_one_terminal_outcome(world, steps, expected):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    job = jobs.acquire_lease(jobs.create(_job_request(world, command)).job_id, timedelta(minutes=30))
    for state in steps:
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    world.recover()
    assert world.jobs().get(job.job_id).job.state is expected


def _job_request(world, command):
    from ayvu.job import JobRequest
    return JobRequest(world.project.project_id, "translate_project", 1, resolve_plan(command, world.project).input_fingerprint, (OUTPUT_ROLE,))


def test_a_job_with_a_live_lease_is_left_alone_and_a_duplicate_is_refused(world):
    command = world.command()
    outcomes = []
    def second_run(text):
        if not outcomes:
            outcomes.append(None)
            outcomes[0] = world.use_case(command).execute(command)
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    duplicate = outcomes[0]
    assert duplicate.job_id is None and duplicate.failure.code == "job_in_progress"
    assert first.state is JobState.VALIDATED
    assert len(world.all_jobs()) == 1


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


def test_a_live_lease_on_this_plan_with_no_live_owner_is_taken_over(world):
    command = world.command()
    left = _job_left_by_a_dead_process(world, command)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    settled = world.jobs().get(left.job_id).job
    assert settled.state is JobState.FAILED and settled.terminal_summary.reason_code == "interrupted"


def test_a_live_lease_on_another_plan_is_left_alone(world):
    command = world.command()
    other = _job_left_by_a_dead_process(world, command, fingerprint="sha256:" + "a" * 64)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    assert world.jobs().get(other.job_id).job.state is JobState.RUNNING


def test_a_queued_job_keeps_its_lease_until_it_expires(world):
    command = world.command()
    queued = _job_left_by_a_dead_process(world, command, state=JobState.QUEUED)

    result = world.use_case(command).execute(command)

    assert result.state is JobState.VALIDATED
    assert world.jobs().get(queued.job_id).job.state is JobState.QUEUED
    world.clock.advance(hours=2)
    assert world.use_case(command).reconcile(command.project_id) == (queued.job_id,)


def test_the_guard_is_given_back_after_ctrl_c(world):
    command = world.command()
    def interrupt(_text):
        raise KeyboardInterrupt
    world.translator.on_call = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.use_case(command).execute(command)
    world.translator.on_call = None

    assert world.use_case(command).execute(command).state is JobState.VALIDATED


def test_a_guard_that_cannot_be_taken_refuses_before_any_job(world):
    command = world.command()
    use_case = world.use_case(command)
    root = world.processing / "projects" / world.project.project_id / "runs"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(b"blocked")

    result = use_case.execute(command)

    assert result.job_id is None and result.failure.code == "run_guard_unavailable"
    assert world.all_jobs() == []


def test_a_job_left_behind_is_settled_before_new_work_starts(world):
    world.ops.after["write"] = world.ops.crash
    with pytest.raises(Crash):
        world.run()
    job = world.all_jobs()[0]
    world.ops.after.clear()
    world.ops.dead = False
    world.clock.advance(hours=2)
    result = world.run()
    assert result.state is JobState.VALIDATED and result.job_id != job.job_id
    assert world.jobs().get(job.job_id).job.state is JobState.FAILED and world.leftovers() == []


def test_a_job_that_gave_up_its_lease_before_ending_is_settled_too(world):
    command = world.command()
    jobs = world.use_case(command)._ports.jobs
    job = jobs.acquire_lease(jobs.create(_job_request(world, command)).job_id, timedelta(minutes=30))
    for state in (JobState.PREFLIGHT, JobState.RUNNING):
        job = jobs.transition(job.job_id, state, expected_revision=job.revision)
    jobs.release_lease(job.job_id, job.lease.owner_token)
    assert world.use_case(command).reconcile(world.project.project_id) == (job.job_id,)
    assert world.jobs().get(job.job_id).job.state is JobState.FAILED


def test_a_job_another_process_moved_is_reported_as_the_record_says(world):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    moved = []

    def on_call(text):
        if not moved:
            job = world.all_jobs()[0]
            moved.append(jobs.transition(job.job_id, JobState.CANCELLING, expected_revision=job.revision))
    world.translator.on_call = on_call
    result = use_case.execute(command)
    assert result.state is JobState.CANCELLING and not result.recorded and not result.output_ready and result.artifact is None


def test_progress_can_never_crowd_out_the_terminal_outcome(world, monkeypatch):
    monkeypatch.setattr("ayvu.translate_project.DEFAULT_EVENT_LOG_CAPACITY", 8)
    result = world.run()
    world.assert_consistent(result)
    assert len(result.events) <= 8 and result.events[-1].payload == result.outcome


def test_a_finished_job_does_not_stop_the_same_work_being_asked_for_again(world):
    first = world.run()
    world.destination.unlink()
    second = world.run()
    assert first.state is JobState.VALIDATED and second.state is JobState.VALIDATED and first.job_id != second.job_id
    assert world.translator.calls and len(set(world.translator.calls)) == len(world.translator.calls)


def test_two_runs_to_one_destination_cannot_both_win(world):
    # Different options, so the second is not a duplicate. It finishes first, while the first one waits on it.
    command = world.command()
    other = world.command(chunk_limit=500)
    finished = []
    def second_run(text):
        if not finished:
            finished.append(None)
            # Its own words, so the two outputs differ.
            world.translator.change = lambda item: f"OTHER:{item}"
            finished[0] = world.use_case(other).execute(other)
            world.translator.change = lambda item: f"PT:{item}"
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    second = finished[0]
    assert second.state is JobState.VALIDATED
    written = world.destination.read_bytes()
    assert first.state is JobState.FAILED and first.failure.code == "output_exists" and first.artifact is None
    assert world.destination.read_bytes() == written and world.leftovers() == []
    assert world.artifacts_of(world.jobs().get(first.job_id).job) == [("abandoned", "final_mismatch")]


def test_a_file_that_appeared_meanwhile_with_the_very_same_bytes_is_not_called_a_failure(world):
    # Reconciliation goes by the bytes, so the run cannot tell this from its own file, and they are the same book.
    command = world.command()
    other = world.command(chunk_limit=500)
    finished = []
    def second_run(text):
        if not finished:
            finished.append(None)
            finished[0] = world.use_case(other).execute(other)
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    assert finished[0].state is JobState.VALIDATED
    assert first.state is JobState.PARTIALLY_COMPLETED and first.outcome.summary.reason_code == "publication_uncommitted"
    assert first.failure.code == "output_exists" and world.leftovers() == []


def test_a_run_that_lost_its_lease_can_never_publish(world):
    world.translator.on_call = lambda text: world.clock.advance(hours=2)
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "lease_expired"
    assert not world.destination.exists() and world.leftovers() == []


def test_a_lease_lost_just_after_the_file_became_visible_ends_with_the_truth_about_the_file(world):
    world.ops.after["link"] = lambda *_: world.clock.advance(hours=2)
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "publication_uncommitted"
    assert result.failure.code == "lease_expired" and result.output_ready
    assert world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(world.all_jobs()[0]) == [("ready", None)]


def test_the_lease_is_renewed_while_a_long_call_blocks(world):
    command = world.command()
    use_case = world.use_case(command, lease_renewal=timedelta(milliseconds=1))
    jobs = use_case._ports.jobs
    renewed = threading.Event()
    original = jobs.renew_lease
    def renew(*args):
        result = original(*args)
        renewed.set()
        return result
    jobs.renew_lease = renew
    def block(text):
        assert renewed.wait(timeout=30)
    world.translator.on_call = block
    assert use_case.execute(command).state is JobState.VALIDATED


def test_a_busy_store_during_renewal_is_tried_again(world):
    command = world.command()
    use_case = world.use_case(command, lease_renewal=timedelta(milliseconds=1))
    jobs = use_case._ports.jobs
    attempts = []
    second = threading.Event()
    def renew(*args):
        attempts.append(1)
        if len(attempts) >= 2:
            second.set()
        raise JobStoreError("busy")
    jobs.renew_lease = renew
    world.translator.on_call = lambda text: second.wait(timeout=30)
    assert use_case.execute(command).state is JobState.VALIDATED and len(attempts) >= 2


def test_lineage_that_cannot_be_written_stops_the_job_being_called_validated(world):
    command = world.command()
    use_case = world.use_case(command)
    class Broken:
        def append(self, request):
            raise ProvenanceStoreError("busy")
    use_case._ports = dataclasses.replace(use_case._ports, provenance=Broken())
    result = use_case.execute(command)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "provenance_failed"
    assert world.destination.exists()


def test_a_store_that_fails_at_the_end_is_reported_as_unrecorded_and_never_as_ready(world):
    command = world.command()
    use_case = world.use_case(command)
    jobs = use_case._ports.jobs
    original = jobs.transition
    def transition(job_id, target, **kwargs):
        if target is JobState.VALIDATED or target is JobState.FAILED:
            raise JobStoreError("busy")
        return original(job_id, target, **kwargs)
    jobs.transition = transition
    result = use_case.execute(command)
    assert not result.recorded and not result.output_ready and result.artifact is None
    assert result.state is JobState.VALIDATING and not world.jobs().get(result.job_id).job.terminal
    world.recover()
    assert world.jobs().get(result.job_id).job.state is JobState.PARTIALLY_COMPLETED


def test_the_state_survives_a_new_process(world):
    result = world.run()
    paths = world.projects.resolve_database_paths(world.project.project_id)
    jobs = open_job_repository(paths.database, paths.backups, world.project.project_id)
    assert jobs.get(result.job_id).job.state is JobState.VALIDATED
    root = world.projects.resolve_artifact_root(world.project.project_id)
    artifacts = open_artifact_service(paths.database, paths.backups, root, world.project.project_id)
    assert artifacts.get(result.artifact.artifact_id).state is ArtifactState.READY
    assert artifacts.find_ready(ArtifactRequest(world.project.project_id, result.job_id, OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION,
                                                result.artifact.request.input_fingerprint)).artifact_id == result.artifact.artifact_id


def test_each_worker_owns_its_translator_and_its_cache(world):
    result = world.run(workers=2)
    assert result.state is JobState.VALIDATED
    assert world.factory_calls["translator"] == world.factory_calls["cache"] - 1 == result.engine.chapters_processed


def test_the_glossary_and_the_cache_order_are_the_current_ones(tmp_path):
    world = World(tmp_path)
    path = tmp_path / "glossary.json"
    path.write_text('{"Second": "Segundo"}', encoding="utf-8")
    world.glossary = load_glossary(path)
    first = world.run()
    assert first.state is JobState.VALIDATED and world.translator.calls
    calls = list(world.translator.calls)
    world.destination.unlink()
    second = world.run()
    # The second run answers from the cache, and the glossary still applies over it.
    assert world.translator.calls == calls and second.engine.stats.from_cache > 0
    assert any("Segundo" in entry.translated for entry in second.engine.review)


def test_a_framework_free_consumer_can_use_the_same_case_with_its_own_ports(world):
    """Nothing here comes from the CLI, a cache or a translator."""
    command = world.command()
    composed = world.use_case(command)
    class Runtime:
        def preflight(self):
            return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_down", retryable=True)
        def facts(self):
            raise AssertionError
        def resources(self):
            raise AssertionError
        def classify(self, error):
            raise AssertionError
    use_case = TranslateProject(dataclasses.replace(composed._ports, runtime=Runtime()), clock=world.clock)
    result = use_case.execute(command)
    world.assert_consistent(result)
    assert isinstance(use_case._ports, TranslateProjectPorts)
    assert result.state is JobState.FAILED and result.failure.code == "provider_down"
    assert result.failure.retryable


def test_a_runtime_that_cannot_classify_still_gives_a_bounded_failure(world):
    command = world.command()
    composed = world.use_case(command)
    runtime = world.runtime_used
    def classify(error):
        raise RuntimeError("broken classifier")
    def resources():
        raise RuntimeError("quoting the book")
    runtime.classify, runtime.resources = classify, resources
    result = composed.execute(command)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "unexpected_error"


def test_nothing_private_reaches_a_repr_an_event_a_record_or_a_log(tmp_path, caplog):
    chapter = XHTML.format(f"<p>{CANARY_TEXT} {CANARY_GLOSSARY}</p>").encode()
    world = World(tmp_path, chapter=chapter)
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({CANARY_GLOSSARY: "Traducaosecreta"}), encoding="utf-8")
    world.glossary = load_glossary(path)
    world.translator.change = lambda text: f"{CANARY_TRANSLATION} {text}"
    with caplog.at_level(logging.DEBUG):
        command = world.command()
        use_case = world.use_case(command)
        result = use_case.execute(command)
    assert result.state is JobState.VALIDATED
    paths = world.projects.resolve_database_paths(world.project.project_id)
    with sqlite3.connect(paths.database) as connection:
        stored = "\n".join(str(row) for table in ("translation_jobs", "job_artifacts", "provenance_records")
                           for row in connection.execute(f"SELECT * FROM {table}"))
    surfaces = [repr(command), repr(result), repr(use_case), repr(result.outcome), repr(result.artifact), repr(result.engine), repr(result.validation),
                repr(world.runtime_used), repr(use_case._ports), repr(resolve_plan(command, world.project)), caplog.text, stored,
                "\n".join(encode_event(event) for event in result.events), "\n".join(repr(event) for event in result.events)]
    for canary in (CANARY_TEXT, CANARY_TRANSLATION, CANARY_URL, CANARY_GLOSSARY, "Traducaosecreta", str(world.destination), str(world.source)):
        for surface in surfaces:
            assert canary not in surface, canary


def test_a_failure_with_a_quoting_message_stays_out_of_every_record(world):
    def change(text):
        raise TranslatorError(f"{CANARY_TEXT} at {CANARY_URL}")
    world.translator.change = change
    result = world.run(fail_fast=True)
    paths = world.projects.resolve_database_paths(world.project.project_id)
    with sqlite3.connect(paths.database) as connection:
        stored = "\n".join(str(row) for table in ("translation_jobs", "provenance_records") for row in connection.execute(f"SELECT * FROM {table}"))
    for surface in (repr(result), repr(result.failure), stored, "\n".join(encode_event(event) for event in result.events)):
        assert CANARY_TEXT not in surface and CANARY_URL not in surface


def test_the_journal_says_where_the_output_went_and_only_that(tmp_path):
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=tmp_path / "out" / "x.epub")
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    journal = tmp_path / "root" / "publications" / f"{artifact}.json"
    assert json.loads(journal.read_text()) == {"version": 1, "destination": str(tmp_path / "out" / "x.epub"), "replace": False}
    assert (journal.stat().st_mode & 0o777) == 0o600
    assert payloads.journal_ids() == (artifact,) and not payloads.publishing
    payloads.publish(artifact, size, digest)
    assert payloads.publishing and payloads.evidence(artifact, size, digest).final == "match"
    with payloads.open_verified(artifact, size, digest) as stream:
        assert stream.read() == b"bytes"
    payloads.forget(artifact)
    payloads.forget(artifact)
    assert payloads.journal_ids() == ()


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 2, "destination": "/x", "replace": false}',
                                     '{"version": 1, "destination": "relative", "replace": false}',
                                     '{"version": 1, "destination": "/x", "replace": "yes"}'])
def test_a_damaged_journal_is_refused_and_never_followed(tmp_path, content):
    from ayvu.artifact import ArtifactPathError
    payloads = LocalDestinationPayloads(tmp_path / "root")
    artifact = "00000000-0000-4000-8000-000000000001"
    journal = tmp_path / "root" / "publications"
    journal.mkdir(parents=True)
    (journal / f"{artifact}.json").write_text(content)
    with pytest.raises(ArtifactPathError):
        payloads.evidence(artifact, 1, "sha256:" + "0" * 64)
    payloads.discard(artifact)
    assert payloads.journal_ids() == ()


def test_a_store_without_a_destination_can_only_settle(tmp_path):
    from ayvu.artifact import ArtifactStoreError
    payloads = LocalDestinationPayloads(tmp_path)
    artifact = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ArtifactStoreError):
        payloads.write_staged(artifact, [b"x"], lambda: None)
    with pytest.raises(ArtifactStoreError):
        payloads.publish(artifact, 1, "sha256:" + "0" * 64)
    assert payloads.evidence(artifact, 1, "sha256:" + "0" * 64).final == "absent"


def test_a_symlink_swapped_in_at_the_destination_does_not_count_as_the_output(tmp_path):
    destination = tmp_path / "out.epub"
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=destination)
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    payloads.publish(artifact, size, digest)
    moved = tmp_path / "moved.epub"
    destination.rename(moved)
    destination.symlink_to(moved)
    from ayvu.artifact import ArtifactPathError
    with pytest.raises(ArtifactPathError):
        payloads.evidence(artifact, size, digest)


def test_an_extra_name_on_the_output_means_it_cannot_be_trusted(tmp_path):
    destination = tmp_path / "out.epub"
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=destination)
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    payloads.publish(artifact, size, digest)
    os.link(destination, tmp_path / "another-name")
    assert payloads.evidence(artifact, size, digest).final == "mismatch"
    assert payloads.evidence(artifact, 99, digest).final == "mismatch"
    destination.unlink()
    assert payloads.evidence(artifact, size, digest).final == "absent"


def test_validation_is_a_value_that_hides_its_text():
    assert "secret" not in repr(OutputValidation(False, ("secret warning",)))
