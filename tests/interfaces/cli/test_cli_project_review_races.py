from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
import pytest
import ayvu.artifacts.destination as artifact_destination
from ayvu.composition.builders import review as review_composition
from ayvu.review import local_files as review_files
from ayvu.review.local_operations import LocalReviewOperations
from ayvu.interfaces.cli.app import app
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository
from ayvu.review import decode_review_package
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobOutcome
from ayvu.review.application import ReviewApplyCommand, ReviewOutcome
from tests.support.cli_translate_project_support import _book, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _mark_reviewed
from tests.support.cli_translate_project_support import _run, runner


def test_destination_race_after_staging_preserves_the_competing_file(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    raw = review_files.read_review_input(review_path)
    output = tmp_path / "reviewed.epub"
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    command = ReviewApplyCommand(raw, None, output, review_files.destination_fingerprint(output))
    staged, race_complete = Event(), Event()
    original_publication = artifact_destination.DestinationPublications.publication

    def pause_after_validation(self, request, owner_token, on_staged):
        def synchronized(staged_path):
            on_staged(staged_path)
            staged.set()
            if not race_complete.wait(5):
                raise RuntimeError("destination race synchronization timed out")

        return original_publication(self, request, owner_token, synchronized)

    monkeypatch.setattr(artifact_destination.DestinationPublications, "publication", pause_after_validation)
    competitor = b"COMPETING_DESTINATION_CANARY"
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(uses.apply.execute, command)
        try:
            assert staged.wait(5)
            output.write_bytes(competitor)
        finally:
            race_complete.set()
        result = future.result(timeout=10)

    assert not result.success and result.outcome is ReviewOutcome.CONFLICT
    assert output.read_bytes() == competitor


def test_repository_revision_race_after_staging_prevents_publication(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    raw = review_files.read_review_input(review_path)
    output = tmp_path / "reviewed.epub"
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    command = ReviewApplyCommand(raw, None, output, review_files.destination_fingerprint(output))
    staged, revision_updated = Event(), Event()
    original_publication = artifact_destination.DestinationPublications.publication

    def pause_after_staging(self, request, owner_token, on_staged):
        def synchronized(staged_path):
            staged.set()
            if not revision_updated.wait(5):
                raise RuntimeError("revision race synchronization timed out")
            on_staged(staged_path)

        return original_publication(self, request, owner_token, synchronized)

    monkeypatch.setattr(artifact_destination.DestinationPublications, "publication", pause_after_staging)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(uses.apply.execute, command)
        try:
            assert staged.wait(5)
            projects = ProjectJsonRepository(tmp_path / "processing")
            project_id = decode_review_package(raw).header.project_id
            current = projects.load_read_only(project_id)
            projects.save(current, expected_revision=current.revision)
        finally:
            revision_updated.set()
        result = future.result(timeout=10)

    assert not result.success and result.code == "stale_review_identity"
    assert not output.exists()


def test_lineage_failure_after_publication_is_partial_without_human_success(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    reviewed_output = tmp_path / "reviewed.epub"
    original_bytes = source.read_bytes()
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    machine_bytes = translated_output.read_bytes()

    def fail_lineage(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_TRANSLATION_CANARY")

    monkeypatch.setattr(LocalReviewOperations, "_write_lineage", fail_lineage)
    applied = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])

    assert applied.exit_code == 1
    assert "provenance_failed" in applied.output
    assert "PRIVATE_TRANSLATION_CANARY" not in applied.output
    assert "Reviewed EPUB saved to:" not in applied.output
    assert reviewed_output.exists()  # D106 committed the file, but the use case did not report success.
    assert source.read_bytes() == original_bytes and translated_output.read_bytes() == machine_bytes
    apply_job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    assert apply_job.state.value == "partially_completed"
    paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(apply_job.request.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, apply_job.request.project_id,
                                            clock=lambda: datetime.now(UTC))
    outcomes = [node.record.request.outcome_code
                for node in provenance.list_by_job(apply_job.job_id, limit=1024).nodes]
    assert "review_applied" not in outcomes and "human_edit_validated" not in outcomes


@pytest.mark.parametrize("event_committed", [False, True], ids=["before-event-commit", "after-event-commit"])
def test_terminal_event_commit_failure_never_invents_a_missing_success_event(
        tmp_path, monkeypatch, event_committed):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    reviewed_output = tmp_path / "reviewed.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)

    original_emit = LocalReviewOperations._emit
    captured_events = []
    interrupt = [True]

    def interrupt_terminal_event(self, state, payload):
        is_apply_outcome = (isinstance(payload, JobOutcome) and payload.state is JobState.VALIDATED
                            and payload.summary.reason_code == "review_applied")
        if interrupt[0] and is_apply_outcome:
            interrupt[0] = False
            if event_committed:
                original_emit(self, state, payload)
            captured_events.extend(state.events.replay())
            raise RuntimeError("simulated event acknowledgement failure")
        return original_emit(self, state, payload)

    monkeypatch.setattr(LocalReviewOperations, "_emit", interrupt_terminal_event)
    applied = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])

    assert reviewed_output.exists()
    assert "PRIVATE_REVIEW" not in applied.output
    if event_committed:
        assert applied.exit_code == 0, applied.output
        assert "Reviewed EPUB saved to:" in applied.output
        assert captured_events[-1].payload.state is JobState.VALIDATED
    else:
        assert applied.exit_code == 1
        assert "job_reconciliation_required" in applied.output
        assert "Reviewed EPUB saved to:" not in applied.output
        assert not any(isinstance(event.payload, JobOutcome) for event in captured_events)
