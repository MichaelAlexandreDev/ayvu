"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import dataclasses
from ayvu.artifacts.model import ArtifactRequest, ArtifactState
from ayvu.knowledge.glossary.rules import load_glossary
from ayvu.jobs.model import JobState
from ayvu.projects.schema import open_artifact_service, open_job_repository
from ayvu.translation.project import TranslateProject
from ayvu.translation.project_contracts import OUTPUT_ROLE, RECONSTRUCT_STAGE, RECONSTRUCT_STAGE_VERSION, TranslateProjectPorts
from ayvu.kernel import JobFailure, JobFailureFamily
from tests.support.translate_project_support import World
from tests.support.translate_project_support import world


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
    assert any("Segundo" in item.text for item in second.engine.translations)


def test_a_framework_free_consumer_can_use_the_same_case_with_its_own_ports(world):
    """Nothing here comes from the CLI, a cache or a translator."""
    command = world.command()
    composed = world.use_case(command)
    class Runtime:
        def preflight(self):
            return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_down", retryable=True)
        def facts(self):
            raise AssertionError
        def resources(self, context):
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
    def resources(context):
        raise RuntimeError("quoting the book")
    runtime.classify, runtime.resources = classify, resources
    result = composed.execute(command)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "unexpected_error"
