"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import dataclasses
import json
import logging
import sqlite3
from dataclasses import replace
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.jobs.model import JobState, JobStoreError
from ayvu.jobs.events import encode_event
from ayvu.provenance.model import ProvenanceStoreError
from ayvu.translation.project_contracts import OutputValidation, resolve_execution_plan as resolve_plan
from ayvu.providers.libretranslate.client import TranslatorError
from tests.support.epub_fakes import XHTML
from tests.support.translate_project_support import CANARY_GLOSSARY, CANARY_TEXT, CANARY_TRANSLATION, CANARY_URL
from tests.support.translate_project_support import World
from tests.support.translate_project_support import _job_request, world


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


def test_validation_is_a_value_that_hides_its_text():
    assert "secret" not in repr(OutputValidation(False, ("secret warning",)))


def test_a_job_queued_with_no_lease_on_this_plan_is_settled_because_its_creator_died(world):
    command = world.command()
    stuck = world.jobs().create(_job_request(world, command))

    assert world.use_case(command).execute(command).state is JobState.VALIDATED

    settled = world.jobs().get(stuck.job_id).job
    assert (settled.state, settled.terminal_summary.reason_code) == (JobState.CANCELLED, "interrupted")


def test_a_job_queued_with_no_lease_on_another_plan_is_left_alone(world):
    command = world.command()
    other = world.jobs().create(replace(_job_request(world, command), input_fingerprint="sha256:" + "a" * 64))
    assert world.use_case(command).execute(command).state is JobState.VALIDATED
    assert world.jobs().get(other.job_id).job.state is JobState.QUEUED
