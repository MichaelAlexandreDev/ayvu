from __future__ import annotations
from dataclasses import replace
from datetime import UTC, datetime
from zipfile import ZipFile
import pytest
from ayvu.composition.builders import review as review_composition, review_files
from ayvu.composition.builders.review_operations import LocalReviewOperations
from ayvu.interfaces.cli.app import app
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository
from ayvu.review import decode_review_package, encode_review_package
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobOutcome
from ayvu.provenance.model import SourceKind
from ayvu.review.application import ReviewApplyCommand, ReviewOutcome
from tests.support.cli_translate_project_support import _apply
from tests.support.cli_translate_project_support import _book, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _mark_reviewed
from tests.support.cli_translate_project_support import _run, _translated_and_reviewed, _tree_snapshot, runner


def test_review_export_validate_apply_and_overwrite_use_shared_application(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    output = tmp_path / "machine.epub"
    review_path = tmp_path / "book-review.csv"
    reviewed_output = tmp_path / "reviewed.epub"
    original_bytes = source.read_bytes()

    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path), source=source, output=output)

    assert translated.exit_code == 0, translated.output
    assert "Review file saved to:" in translated.output
    package_raw = review_path.read_bytes()
    assert package_raw.startswith(b"AYVU-REVIEW-PACKAGE,2,")
    package = decode_review_package(package_raw)
    edited = replace(package, rows=tuple(
        replace(row,
                reviewed_text=("PRIVATE_REVIEW_TEXT_CANARY_149 " if index == 0 else "")
                + row.machine_translation.replace("PT:", "HUMAN:"),
                reviewer_note="PRIVATE_REVIEW_NOTE_CANARY_149" if index == 0 else row.reviewer_note,
                review_status="reviewed")
        for index, row in enumerate(package.rows)))
    review_path.write_bytes(encode_review_package(edited))
    machine_bytes = output.read_bytes()
    processing = tmp_path / "processing"
    before_validation = _tree_snapshot(processing)

    checked = runner.invoke(app, ["apply-review", str(source), str(review_path), "--validate-only"])

    assert checked.exit_code == 0, checked.output
    assert "Review package validation" in " ".join(checked.output.split())
    assert not reviewed_output.exists()
    assert _tree_snapshot(processing) == before_validation

    machine_alias = runner.invoke(
        app, ["apply-review", str(source), str(review_path), "--output", str(output), "--overwrite"])
    assert machine_alias.exit_code == 1
    assert "output_is_machine_artifact" in machine_alias.output
    assert output.read_bytes() == machine_bytes

    applied = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])
    assert applied.exit_code == 0, applied.output
    assert "Review application report" in applied.output
    assert "Reviewed EPUB saved to:" in applied.output
    assert "PRIVATE_REVIEW_TEXT_CANARY_149" not in applied.output
    assert "PRIVATE_REVIEW_NOTE_CANARY_149" not in applied.output
    with ZipFile(reviewed_output) as archive:
        assert b"HUMAN:Hello reader." in archive.read("OEBPS/c1.xhtml")
    apply_job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    project_store = ProjectJsonRepository(processing)
    apply_paths = project_store.resolve_database_paths(apply_job.request.project_id)
    review_provenance = open_provenance_repository(apply_paths.database, apply_paths.backups,
                                                   apply_job.request.project_id,
                                                   clock=lambda: datetime.now(UTC))
    review_records = [node.record for node in review_provenance.list_by_job(apply_job.job_id, limit=1024).nodes]
    package_records = [record for record in review_records if record.request.outcome_code == "review_package_validated"]
    unit_records = [record for record in review_records if record.request.operation == "review_unit"]
    commit_records = [record for record in review_records if record.request.outcome_code == "review_applied"]
    assert len(package_records) == len(commit_records) == 1 and unit_records
    assert all(record.request.source_kind is SourceKind.HUMAN
               and record.request.outcome_code == "human_edit_validated" for record in unit_records)
    assert all(package_records[0].record_id in record.request.parents for record in unit_records)
    assert {package_records[0].record_id, next(record.record_id for record in review_records
                                               if record.request.outcome_code == "source_verified")} <= set(commit_records[0].request.parents)
    assert not any(record.request.outcome_code == "human_edit_applied" for record in review_records)
    persistent_projection = apply_paths.database.read_bytes() + repr(review_records).encode()
    assert b"PRIVATE_REVIEW_TEXT_CANARY_149" not in persistent_projection
    assert b"PRIVATE_REVIEW_NOTE_CANARY_149" not in persistent_projection
    assert str(review_path).encode() not in persistent_projection
    assert source.read_bytes() == original_bytes
    assert output.read_bytes() == machine_bytes


def test_apply_rechecks_project_revision_after_side_effect_free_preview(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    package = decode_review_package(review_path.read_bytes())
    reviewed = replace(package, rows=tuple(
        replace(row, review_status="reviewed") for row in package.rows))
    review_path.write_bytes(encode_review_package(reviewed))

    preview = runner.invoke(app, ["apply-review", str(source), str(review_path), "--validate-only"])
    assert preview.exit_code == 0, preview.output
    projects = ProjectJsonRepository(tmp_path / "processing")
    project_id = _jobs(tmp_path)[0].request.project_id
    current = projects.load_read_only(project_id)
    projects.save(current, expected_revision=current.revision)
    output = tmp_path / "stale-reviewed.epub"

    applied = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(output)])

    assert applied.exit_code == 1
    assert "stale_project_revision" in applied.output
    assert not output.exists()


def test_an_identical_apply_with_its_result_intact_changes_nothing(tmp_path, monkeypatch):
    source, review_path, output = _translated_and_reviewed(tmp_path, monkeypatch)
    assert _apply(source, review_path, output).exit_code == 0
    first, modified = output.read_bytes(), output.stat().st_mtime_ns

    again = _apply(source, review_path, output)

    assert again.exit_code == 0, again.output
    assert (output.read_bytes(), output.stat().st_mtime_ns) == (first, modified)


def test_apply_runs_again_when_the_person_removed_the_reviewed_file(tmp_path, monkeypatch):
    source, review_path, output = _translated_and_reviewed(tmp_path, monkeypatch)
    assert _apply(source, review_path, output).exit_code == 0
    first = output.read_bytes()
    output.unlink()

    again = _apply(source, review_path, output)

    assert again.exit_code == 0, again.output
    assert output.read_bytes() == first
    # Twice, because the second run must itself be replayable and not collide with the first one's lineage.
    assert _apply(source, review_path, output).exit_code == 0


def test_a_changed_reviewed_file_is_not_replaced_without_overwrite(tmp_path, monkeypatch):
    source, review_path, output = _translated_and_reviewed(tmp_path, monkeypatch)
    assert _apply(source, review_path, output).exit_code == 0
    first = output.read_bytes()
    output.write_bytes(b"my own notes")

    refused = _apply(source, review_path, output)

    assert refused.exit_code == 1
    assert "output_exists" in refused.output
    assert output.read_bytes() == b"my own notes"

    replaced = _apply(source, review_path, output, "--overwrite")

    assert replaced.exit_code == 0, replaced.output
    assert output.read_bytes() == first


@pytest.mark.parametrize("safe_point", ["review_before_validation", "review_before_reconstruction"])
def test_cancelling_an_apply_against_real_repositories_is_recorded_and_frees_the_next_one(tmp_path, monkeypatch, safe_point):
    source, review_path, output = _translated_and_reviewed(tmp_path, monkeypatch)
    raw = review_files.read_review_input(review_path)
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    cancellation = uses.cancellation()

    class CancelAt:
        """Requests at one declared safe point, so everything before it really ran."""

        def checkpoint(self, point):
            if point == safe_point:
                cancellation.request("user_requested", datetime.now(UTC))
            return cancellation.token.checkpoint(point)

    cancelled = uses.apply.execute(ReviewApplyCommand(
        raw, None, output, review_files.destination_fingerprint(output), cancellation=CancelAt()))

    assert (cancelled.outcome, cancelled.code, cancelled.success) == (ReviewOutcome.CANCELLED, "cancelled", False)
    assert not output.exists()
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    assert job.state is JobState.CANCELLED
    assert [type(event.payload).__name__ for event in cancelled.events][-3:] == [
        "CancellationRequest", "CancellationAcknowledgement", "JobOutcome"]

    # The guard is released and the attempt left nothing that blocks a new one.
    again = _apply(source, review_path, output)
    assert again.exit_code == 0, again.output


def test_a_lost_failure_event_does_not_keep_the_destination_locked(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=tmp_path / "machine.epub")
    assert translated.exit_code == 0, translated.output
    output = tmp_path / "reviewed.epub"
    original_emit = LocalReviewOperations._emit

    def lose_the_outcome(self, state, payload):
        if type(payload) is JobOutcome:
            raise RuntimeError("event store unavailable")
        return original_emit(self, state, payload)

    with monkeypatch.context() as patch:
        patch.setattr(LocalReviewOperations, "_emit", lose_the_outcome)
        failed = _apply(source, review_path, output)
    assert failed.exit_code == 1
    assert not output.exists()

    _mark_reviewed(review_path)
    again = _apply(source, review_path, output)

    assert again.exit_code == 0, again.output
