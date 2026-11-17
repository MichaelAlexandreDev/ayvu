"""REF-18 step 4, answer P1: a resume keeps the runtime its job started with."""
from __future__ import annotations

from ayvu.interfaces.cli import app as cli
from ayvu.jobs import JobState
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs

FAILING = "Goodbye reader."


def flat(result):
    return " ".join(text(result).split())


def resume(tmp_path, monkeypatch):
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    return runner.invoke(cli.app, ["--mode", "developer", "resume", str(tmp_path / "book.epub")]), session


def interrupted(tmp_path, monkeypatch, label):
    # v1 without --fail-fast publishes a partial EPUB and leaves nothing to resume.
    config(tmp_path, label=label)
    if label is None:
        from tests.support.job_recovery_integration_support import Scene
        scene = Scene(tmp_path, url="http://localhost:5000")
        scene.interrupted_state()
    else:
        invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--fail-fast", session=Transport(failing=FAILING))
    return _jobs(tmp_path)[-1]


def test_a_v1_job_finishes_on_v1_after_a_label_is_declared(tmp_path, monkeypatch):
    parent = interrupted(tmp_path, monkeypatch, None)
    assert parent.request.command_version == 1
    config(tmp_path, label="local-model-2026")
    result, session = resume(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == parent.job_id)
    assert child.request.command_version == 1 and child.state is JobState.VALIDATED
    assert any(payload and payload.get("q") == FAILING for _, _, payload in session.requests)
    assert (tmp_path / "out.epub").is_file()


def test_a_rich_job_resumes_with_the_label_it_started_with(tmp_path, monkeypatch):
    parent = interrupted(tmp_path, monkeypatch, "first-model")
    assert parent.state is JobState.FAILED and parent.request.command_version == 2
    result, _ = resume(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == parent.job_id)
    assert child.state is JobState.VALIDATED
    assert all(record.producer.model == "declared:first-model" for record in records(tmp_path, child))


def test_a_rich_job_resumed_with_another_label_translates_again_and_says_why(tmp_path, monkeypatch):
    parent = interrupted(tmp_path, monkeypatch, "first-model")
    config(tmp_path, label="second-model")
    result, _ = resume(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    assert "raw_results_changed_provider" in flat(result)
    child = next(job for job in _jobs(tmp_path) if job.continuation_of == parent.job_id)
    assert child.state is JobState.VALIDATED and child.request.command_version == 2
    assert all(record.producer.model == "declared:second-model" for record in records(tmp_path, child))


def test_a_rich_job_without_any_label_explains_how_to_resume(tmp_path, monkeypatch):
    parent = interrupted(tmp_path, monkeypatch, "first-model")
    config(tmp_path, label=None)
    result, _ = resume(tmp_path, monkeypatch)
    assert result.exit_code == 1
    assert "Um job rich/v2 só pode ser retomado com um rótulo de modelo." in flat(result)
    assert [job.job_id for job in _jobs(tmp_path)] == [parent.job_id]
