"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import dataclasses
import os
from pathlib import Path
import pytest
from ayvu.artifacts.model import ArtifactState
from ayvu.jobs.model import JobState, JobStoreError
from ayvu.jobs.events import JobEventKind, encode_event
from ayvu.translation.project_contracts import TranslateCommandError, TranslateObserver, resolve_execution_plan as resolve_plan
from ayvu.composition.builders.translate_project import LegacyOutputValidator, LegacyResources
from ayvu.providers.libretranslate.client import TranslatorError
from tests.support.epub_fakes import XHTML
from ayvu.kernel import ChapterSelection, LanguagePair, OutputPlan, TranslationMemoryOptions, TranslationOptions, JobFailureFamily
from tests.support.translate_project_support import SCENARIOS, World
from tests.support.translate_project_support import current_flow, world


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
                world.command(destination=world.tmp_path / "other.epub"),
                world.command(translation_memory=TranslationMemoryOptions()),
                world.command(translation_memory=TranslationMemoryOptions(conservative_auto_apply=True)),
                world.command(translation_memory=TranslationMemoryOptions(legacy_cache_compatibility=True))]
    seen = {resolve_plan(item, world.project).input_fingerprint for item in variants}
    assert len(seen) == len(variants) and fingerprint not in seen
    assert resolve_plan(base, dataclasses.replace(world.project, glossary_ref="glossary:" + "a" * 64)).input_fingerprint != fingerprint
    assert resolve_plan(base, dataclasses.replace(world.project, configuration_ref="configuration:" + "b" * 64)).input_fingerprint != fingerprint
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolve_plan(base, world.project).input_fingerprint = "x"
