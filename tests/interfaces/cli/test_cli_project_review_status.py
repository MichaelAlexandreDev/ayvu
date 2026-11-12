from __future__ import annotations
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from zipfile import ZipFile
import pytest
from ayvu.composition.builders import review as review_composition, review_files
from ayvu.composition.builders.review_operations import LocalReviewOperations
from ayvu.interfaces.cli.app import app
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository
from ayvu.review import decode_review_package, encode_review_package
from ayvu.review.application import ReviewApplyCommand, ReviewOutcome
from tests.support.cli_translate_project_support import _apply
from tests.support.cli_translate_project_support import _book, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _mark_reviewed
from tests.support.cli_translate_project_support import _run, _translated_and_reviewed, _translated_with_a_package, runner


def test_reviewing_one_line_applies_it_and_keeps_the_machine_translation_elsewhere(tmp_path, monkeypatch):
    source, review_path, output = _translated_with_a_package(tmp_path, monkeypatch)
    package = decode_review_package(review_path.read_bytes())
    target = next(row for row in package.rows if "Hello reader" in row.machine_translation)
    edited = replace(target, reviewed_text=target.machine_translation.replace("PT:", "HUMAN:"), review_status="reviewed")
    review_path.write_bytes(encode_review_package(replace(package, rows=tuple(
        edited if row is target else row for row in package.rows))))

    checked = runner.invoke(app, ["apply-review", str(source), str(review_path), "--validate-only"])
    applied = _apply(source, review_path, output)

    assert checked.exit_code == 0, checked.output
    assert applied.exit_code == 0, applied.output
    with ZipFile(output) as archive:
        chapter = archive.read("OEBPS/c1.xhtml").decode()
    assert "HUMAN:Hello reader." in chapter
    assert "PT:Hello title" in chapter
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == "apply_review_package")
    paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(job.request.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, job.request.project_id, clock=lambda: datetime.now(UTC))
    units = [node.record for node in provenance.list_by_job(job.job_id, limit=1024).nodes
             if node.record.request.operation == "review_unit"]
    assert [record.request.subject.value for record in units] == [target.unit_id]


def test_an_edit_left_unmarked_stops_the_package_and_says_so(tmp_path, monkeypatch):
    source, review_path, output = _translated_with_a_package(tmp_path, monkeypatch)
    package = decode_review_package(review_path.read_bytes())
    edited = replace(package.rows[0], reviewed_text=package.rows[0].machine_translation + " changed")
    review_path.write_bytes(encode_review_package(replace(package, rows=(edited, *package.rows[1:]))))

    refused = _apply(source, review_path, output)

    assert refused.exit_code == 1
    assert "pending_row_edited" in refused.output
    assert "não foi marcada" in refused.output
    assert not output.exists()


def test_a_rejected_line_stops_the_package_and_says_so(tmp_path, monkeypatch):
    source, review_path, output = _translated_with_a_package(tmp_path, monkeypatch)
    package = decode_review_package(review_path.read_bytes())
    rejected = replace(package.rows[0], review_status="rejected")
    review_path.write_bytes(encode_review_package(replace(package, rows=(rejected, *package.rows[1:]))))

    refused = _apply(source, review_path, output)

    assert refused.exit_code == 1
    assert "review_rejected" in refused.output
    assert not output.exists()


def test_a_package_nobody_approved_says_what_to_mark(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=tmp_path / "machine.epub")
    assert translated.exit_code == 0, translated.output
    output = tmp_path / "reviewed.epub"

    refused = _apply(source, review_path, output)

    assert refused.exit_code == 1
    assert "review_not_approved" in refused.output
    assert "review_status" in refused.output
    assert not output.exists()


@pytest.mark.parametrize("prefix,delimiter", [(b"\xef\xbb\xbf", ","), (b"", ";")], ids=["bom", "semicolon"])
def test_a_package_saved_by_a_spreadsheet_applies_through_the_command(tmp_path, monkeypatch, prefix, delimiter):
    source, review_path, output = _translated_and_reviewed(tmp_path, monkeypatch)
    rows = list(csv.reader(io.StringIO(review_path.read_bytes().decode("utf-8"), newline="")))
    resaved = io.StringIO(newline="")
    csv.writer(resaved, delimiter=delimiter, lineterminator="\n").writerows(rows)
    review_path.write_bytes(prefix + resaved.getvalue().encode("utf-8"))

    applied = _apply(source, review_path, output)

    assert applied.exit_code == 0, applied.output
    assert output.exists()


def test_concurrent_apply_of_the_same_request_is_serialized(tmp_path, monkeypatch):
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
    entered_reconstruction, release_reconstruction = Event(), Event()
    original_reconstruct = LocalReviewOperations.reconstruct

    def pause_first(self, *args, **kwargs):
        entered_reconstruction.set()
        if not release_reconstruction.wait(5):
            raise RuntimeError("concurrent test synchronization timed out")
        return original_reconstruct(self, *args, **kwargs)

    monkeypatch.setattr(LocalReviewOperations, "reconstruct", pause_first)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first_future = workers.submit(uses.apply.execute, command)
        try:
            assert entered_reconstruction.wait(5)
            second = uses.apply.execute(command)
        finally:
            release_reconstruction.set()
        first = first_future.result(timeout=10)

    assert second.outcome is ReviewOutcome.CONFLICT and second.code == "job_in_progress"
    assert first.success and output.exists()
