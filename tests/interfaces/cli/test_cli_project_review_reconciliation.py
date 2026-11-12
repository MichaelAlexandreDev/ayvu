from __future__ import annotations
from dataclasses import replace
from datetime import UTC, datetime
from zipfile import ZipFile
import ayvu.artifacts.destination as artifact_destination
import ayvu.jobs.store as job_store
from ayvu.review.local_operations import LocalReviewOperations
from ayvu.interfaces.cli.app import app
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository
from ayvu.review import decode_review_package, encode_review_package
from ayvu.jobs.model import JobState
from tests.support.cli_translate_project_support import _book, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _mark_reviewed
from tests.support.cli_translate_project_support import _run, runner


def test_final_lineage_marker_reconciles_job_commit_after_restart(tmp_path, monkeypatch):
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
    original_finish = LocalReviewOperations._finish
    interrupt = [True]

    def fail_job_commit(self, state, target, code, failure, counts, **kwargs):
        if interrupt[0] and target is JobState.VALIDATED and code == "review_applied":
            interrupt[0] = False
            raise RuntimeError("simulated repository commit interruption")
        return original_finish(self, state, target, code, failure, counts, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(LocalReviewOperations, "_finish", fail_job_commit)
        interrupted = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output",
                                          str(reviewed_output)])

    assert interrupted.exit_code == 1
    assert "job_reconciliation_required" in interrupted.output
    assert reviewed_output.exists()
    pending_job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    assert pending_job.state is JobState.VALIDATING

    recovered = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output",
                                    str(reviewed_output)])

    assert recovered.exit_code == 0, recovered.output
    assert "Reviewed EPUB saved to:" in recovered.output
    recovered_job = next(job for job in _jobs(tmp_path) if job.job_id == pending_job.job_id)
    assert recovered_job.state is JobState.VALIDATED

    # An identical request is idempotent even though its output now exists.
    first_reviewed_bytes = reviewed_output.read_bytes()
    replay = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])
    assert replay.exit_code == 0, replay.output
    assert reviewed_output.read_bytes() == first_reviewed_bytes

    old_reviewed_bytes = reviewed_output.read_bytes()
    edited = decode_review_package(review_path.read_bytes())
    edited_again = replace(edited, rows=tuple(
        replace(row, reviewed_text=row.reviewed_text.replace("HUMAN:", "HUMAN-2:")) for row in edited.rows))
    review_path.write_bytes(encode_review_package(edited_again))
    conflict = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])
    assert conflict.exit_code == 1
    assert "output_exists" in conflict.output
    assert reviewed_output.read_bytes() == old_reviewed_bytes

    overwritten = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output",
                                      str(reviewed_output), "--overwrite"])
    assert overwritten.exit_code == 0, overwritten.output
    with ZipFile(reviewed_output) as archive:
        assert b"HUMAN-2:Hello reader." in archive.read("OEBPS/c1.xhtml")
    assert source.read_bytes() == original_bytes
    assert translated_output.read_bytes() == machine_bytes


def test_terminal_job_repository_commit_ack_loss_reconciles_without_false_success(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    reviewed_output = tmp_path / "reviewed.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    original_transition = job_store.SqliteJobRepository.transition
    interrupt = [True]

    def commit_then_interrupt(self, job_id, target, *, expected_revision, summary=None):
        committed = original_transition(self, job_id, target, expected_revision=expected_revision, summary=summary)
        if interrupt[0] and target is JobState.VALIDATED:
            interrupt[0] = False
            raise RuntimeError("simulated lost repository commit acknowledgement")
        return committed

    monkeypatch.setattr(job_store.SqliteJobRepository, "transition", commit_then_interrupt)
    first = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])

    assert first.exit_code == 1
    assert "job_reconciliation_required" in first.output
    assert "Reviewed EPUB saved to:" not in first.output
    assert reviewed_output.exists()
    apply_job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    assert apply_job.state is JobState.VALIDATED

    recovered = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(reviewed_output)])

    assert recovered.exit_code == 0, recovered.output
    assert "Reviewed EPUB saved to:" in recovered.output


def test_rename_before_artifact_commit_is_reconciled_without_a_false_review_success(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    reviewed_output = tmp_path / "reviewed.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    reviewed_output.write_bytes(b"prior-reviewed-artifact")
    original_replace = artifact_destination.DestinationFileOps.replace
    interrupt = [True]

    def rename_then_interrupt(self, staged, target):
        original_replace(self, staged, target)
        if interrupt[0]:
            interrupt[0] = False
            raise RuntimeError("simulated crash after rename")

    monkeypatch.setattr(artifact_destination.DestinationFileOps, "replace", rename_then_interrupt)
    first = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output",
                                str(reviewed_output), "--overwrite"])

    assert first.exit_code == 1
    assert "Reviewed EPUB saved to:" not in first.output
    with ZipFile(reviewed_output) as archive:
        assert b"HUMAN:Hello reader." in archive.read("OEBPS/c1.xhtml")
    apply_jobs = [job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package"]
    assert len(apply_jobs) == 1 and apply_jobs[0].state is not JobState.VALIDATED
    paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(apply_jobs[0].request.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, apply_jobs[0].request.project_id,
                                            clock=lambda: datetime.now(UTC))
    outcomes = [node.record.request.outcome_code
                for node in provenance.list_by_job(apply_jobs[0].job_id, limit=1024).nodes]
    assert "review_applied" not in outcomes and "human_edit_validated" not in outcomes

    second = runner.invoke(app, ["apply-review", str(source), str(review_path), "--output",
                                 str(reviewed_output), "--overwrite"])

    assert second.exit_code == 0, second.output
    assert "Reviewed EPUB saved to:" in second.output
    apply_jobs = [job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package"]
    assert any(job.state is JobState.VALIDATED for job in apply_jobs)
