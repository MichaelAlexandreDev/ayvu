"""The real ``translate`` command through the use case, watched from the job record and the files it leaves."""
from __future__ import annotations

import csv
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from zipfile import ZipFile, ZipInfo

import pytest
import requests
from typer.testing import CliRunner

import ayvu.artifact_destination as artifact_destination
import ayvu.cli as cli_module
import ayvu.job_store as job_store
import ayvu.review_composition as review_composition
import ayvu.translate_project_composition as composition
from ayvu.cli import _TRANSLATION_FAILURES, _BookOutcome, _print_translation_failure, app
from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions, UserMode
from ayvu.project_schema import open_job_repository, open_provenance_repository
from ayvu.preflight import PreflightError
from ayvu.project_store import ProjectJsonRepository, build_project_request
from ayvu.resume import ResumeStateStore
from ayvu.review_exchange import decode_review_package, encode_review_package
from ayvu.epub_io import TranslationReport
from ayvu.job import JobError, JobState, JobTerminalSummary
from ayvu.job_events import JobFailure, JobFailureFamily, JobOutcome
from ayvu.provenance import SourceKind
from ayvu.review_application import ReviewApplyCommand, ReviewOutcome
from ayvu.translate_project import TRANSLATE_COMMAND_TYPE, OutputValidation, TranslateObserver, TranslateProjectResult
from ayvu.translate_project_composition import ProjectTranslationRun

runner = CliRunner()
LANGUAGES = [{"code": "en", "name": "English", "targets": ["pt"]}, {"code": "pt", "name": "Portuguese", "targets": []}]
ENCRYPTED_DOCUMENT = (
    '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:e="http://www.w3.org/2001/04/xmlenc#">'
    '<e:EncryptedData><e:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>'
    '<e:CipherData><e:CipherReference URI="OEBPS/c1.xhtml"/></e:CipherData></e:EncryptedData></encryption>'
)


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ayvu.cli._processing_dir", lambda _config: tmp_path / "processing")


class Session:
    """An in-process LibreTranslate. ``on_post`` runs before each answer, and may raise or change the world."""

    def __init__(self, failing: str | None = None, on_post=None) -> None:
        self.posts: list[str] = []
        self.failing = failing
        self.on_post = on_post

    @staticmethod
    def _response(payload: object, status: int = 200) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response.url = "http://translator.test"
        response._content = json.dumps(payload).encode()
        return response

    def get(self, _url: str, *, timeout: float) -> requests.Response:
        return self._response(LANGUAGES)

    def post(self, _url: str, *, json: dict[str, str], timeout: float) -> requests.Response:
        self.posts.append(json["q"])
        if self.on_post is not None:
            self.on_post(json["q"])
        if self.failing and self.failing in json["q"]:
            return self._response({"error": "no"}, 500)
        return self._response({"translatedText": f"PT:{json['q']}"})

    def close(self) -> None:
        pass


def _document(body: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head><body>{body}</body></html>'


def _book(path: Path, *, chapter_two: str = "<p>Goodbye reader.</p>", extra: tuple[tuple[str, str], ...] = ()) -> Path:
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'unique-identifier="b" version="3.0"><metadata><dc:identifier id="b">x</dc:identifier><dc:title>My Title</dc:title>'
        '<dc:language>en</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    members = (("META-INF/container.xml", container), ("OEBPS/content.opf", opf),
               ("OEBPS/nav.xhtml", _document('<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol><li><a href="c1.xhtml">One</a></li></ol></nav>')),
               ("OEBPS/c1.xhtml", _document("<h1>Hello title</h1><p>Hello reader.</p>")), ("OEBPS/c2.xhtml", _document(chapter_two)), *extra)
    with ZipFile(path, "w") as archive:
        archive.writestr(ZipInfo("mimetype", (2024, 1, 1, 0, 0, 0)), b"application/epub+zip")
        for name, content in members:
            archive.writestr(ZipInfo(name, (2024, 1, 1, 0, 0, 0)), content)
    return path


def _args(tmp_path: Path, source: Path, *more: str, output: Path | None = None) -> list[str]:
    return ["--mode", "developer", "translate", str(source), "--source", "en", "--target", "pt", "--url", "http://translator.test",
            "--retries", "0", "--cache", str(tmp_path / "cache.sqlite"), "--output", str(output or tmp_path / "out.epub"), *more]


def _run(tmp_path, monkeypatch, *more, source=None, session=None, output=None, mode_args=None, stdin=None):
    session = session or Session()
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: session)
    source = source or _book(tmp_path / "book.epub")
    return runner.invoke(app, mode_args or _args(tmp_path, source, *more, output=output), input=stdin), session


def _jobs(tmp_path: Path) -> list:
    projects = ProjectJsonRepository(tmp_path / "processing")
    found = []
    for manifest in sorted((tmp_path / "processing" / "projects").glob("*/project.json")):
        project_id = json.loads(manifest.read_text())["project_id"]
        paths = projects.resolve_database_paths(project_id)
        jobs = open_job_repository(paths.database, paths.backups, project_id, clock=lambda: datetime.now(UTC))
        found.extend(item.job for item in jobs.list_jobs(project_id))
    return found


def _only_job(tmp_path: Path):
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1 and jobs[0].request.command_type == TRANSLATE_COMMAND_TYPE
    return jobs[0]


def _leftovers(directory: Path) -> list[str]:
    return [path.name for path in directory.iterdir() if path.name.startswith(".ayvu-")]


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def _mark_reviewed(path: Path, prefix: str = "HUMAN:") -> None:
    package = decode_review_package(path.read_bytes())
    reviewed = replace(package, rows=tuple(
        replace(row, reviewed_text=row.machine_translation.replace("PT:", prefix), review_status="reviewed")
        for row in package.rows))
    path.write_bytes(encode_review_package(reviewed))


def test_a_translation_ends_as_a_validated_job_with_the_output_in_place(tmp_path, monkeypatch):
    result, session = _run(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    job = _only_job(tmp_path)
    assert job.state.value == "validated" and job.terminal_summary.reason_code == "validated"
    assert dict(job.terminal_summary.counters)["translated"] == len(session.posts) > 0
    with ZipFile(tmp_path / "out.epub") as archive:
        assert b"PT:Hello title" in archive.read("OEBPS/c1.xhtml")
    assert not _leftovers(tmp_path)


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


def _translated_and_reviewed(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=tmp_path / "machine.epub")
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    return source, review_path, tmp_path / "reviewed.epub"


def _apply(source: Path, review_path: Path, output: Path, *more: str):
    return runner.invoke(app, ["apply-review", str(source), str(review_path), "--output", str(output), *more])


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
    raw = review_composition.read_review_input(review_path)
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    cancellation = uses.cancellation()

    class CancelAt:
        """Requests at one declared safe point, so everything before it really ran."""

        def checkpoint(self, point):
            if point == safe_point:
                cancellation.request("user_requested", datetime.now(UTC))
            return cancellation.token.checkpoint(point)

    cancelled = uses.apply.execute(ReviewApplyCommand(
        raw, None, output, review_composition.destination_fingerprint(output), cancellation=CancelAt()))

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
    original_emit = review_composition.LocalReviewOperations._emit

    def lose_the_outcome(self, state, payload):
        if type(payload) is JobOutcome:
            raise RuntimeError("event store unavailable")
        return original_emit(self, state, payload)

    with monkeypatch.context() as patch:
        patch.setattr(review_composition.LocalReviewOperations, "_emit", lose_the_outcome)
        failed = _apply(source, review_path, output)
    assert failed.exit_code == 1
    assert not output.exists()

    _mark_reviewed(review_path)
    again = _apply(source, review_path, output)

    assert again.exit_code == 0, again.output


def _translated_with_a_package(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=tmp_path / "machine.epub")
    assert translated.exit_code == 0, translated.output
    return source, review_path, tmp_path / "reviewed.epub"


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
    raw = review_composition.read_review_input(review_path)
    output = tmp_path / "reviewed.epub"
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    command = ReviewApplyCommand(raw, None, output, review_composition.destination_fingerprint(output))
    entered_reconstruction, release_reconstruction = Event(), Event()
    original_reconstruct = review_composition.LocalReviewOperations.reconstruct

    def pause_first(self, *args, **kwargs):
        entered_reconstruction.set()
        if not release_reconstruction.wait(5):
            raise RuntimeError("concurrent test synchronization timed out")
        return original_reconstruct(self, *args, **kwargs)

    monkeypatch.setattr(review_composition.LocalReviewOperations, "reconstruct", pause_first)
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


def test_destination_race_after_staging_preserves_the_competing_file(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated_output = tmp_path / "machine.epub"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=translated_output)
    assert translated.exit_code == 0, translated.output
    _mark_reviewed(review_path)
    raw = review_composition.read_review_input(review_path)
    output = tmp_path / "reviewed.epub"
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    command = ReviewApplyCommand(raw, None, output, review_composition.destination_fingerprint(output))
    staged, race_complete = Event(), Event()
    original_publication = review_composition.DestinationPublications.publication

    def pause_after_validation(self, request, owner_token, on_staged):
        def synchronized(staged_path):
            on_staged(staged_path)
            staged.set()
            if not race_complete.wait(5):
                raise RuntimeError("destination race synchronization timed out")

        return original_publication(self, request, owner_token, synchronized)

    monkeypatch.setattr(review_composition.DestinationPublications, "publication", pause_after_validation)
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
    raw = review_composition.read_review_input(review_path)
    output = tmp_path / "reviewed.epub"
    uses = review_composition.open_review_use_cases(tmp_path / "processing", source_path=source)
    command = ReviewApplyCommand(raw, None, output, review_composition.destination_fingerprint(output))
    staged, revision_updated = Event(), Event()
    original_publication = review_composition.DestinationPublications.publication

    def pause_after_staging(self, request, owner_token, on_staged):
        def synchronized(staged_path):
            staged.set()
            if not revision_updated.wait(5):
                raise RuntimeError("revision race synchronization timed out")
            on_staged(staged_path)

        return original_publication(self, request, owner_token, synchronized)

    monkeypatch.setattr(review_composition.DestinationPublications, "publication", pause_after_staging)
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

    monkeypatch.setattr(review_composition.LocalReviewOperations, "_write_lineage", fail_lineage)
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

    original_emit = review_composition.LocalReviewOperations._emit
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

    monkeypatch.setattr(review_composition.LocalReviewOperations, "_emit", interrupt_terminal_event)
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
    original_finish = review_composition.LocalReviewOperations._finish
    interrupt = [True]

    def fail_job_commit(self, state, target, code, failure, counts, **kwargs):
        if interrupt[0] and target is JobState.VALIDATED and code == "review_applied":
            interrupt[0] = False
            raise RuntimeError("simulated repository commit interruption")
        return original_finish(self, state, target, code, failure, counts, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(review_composition.LocalReviewOperations, "_finish", fail_job_commit)
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


def test_validation_warnings_keep_the_output_and_end_partially_completed(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub", chapter_two='<p><a href="gone.xhtml">Broken</a></p>')
    result, _ = _run(tmp_path, monkeypatch, source=source)

    assert result.exit_code == 1
    assert (tmp_path / "out.epub").exists()
    job = _only_job(tmp_path)
    assert (job.state.value, job.terminal_summary.reason_code) == ("partially_completed", "validation_warnings")
    state = ResumeStateStore(tmp_path / "processing").scan().running
    assert len(state) == 1


def test_a_failed_text_keeps_the_exit_code_and_ends_partially_completed(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, session=Session(failing="Goodbye reader."))

    assert result.exit_code == 0, result.output
    job = _only_job(tmp_path)
    assert (job.state.value, job.terminal_summary.reason_code) == ("partially_completed", "texts_failed")


def test_require_full_cache_ends_without_an_output_or_a_failed_job(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, "--cache-only", "--require-full-cache")

    assert result.exit_code == 1
    assert not (tmp_path / "out.epub").exists()
    job = _only_job(tmp_path)
    assert (job.state.value, job.terminal_summary.reason_code) == ("partially_completed", "cache_incomplete")


def test_fail_fast_says_what_happened_without_a_traceback(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, "--fail-fast", session=Session(failing="Goodbye reader."))

    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "--fail-fast" in result.output and "cache" in result.output
    assert not (tmp_path / "out.epub").exists() and not _leftovers(tmp_path)
    assert _only_job(tmp_path).state.value == "failed"


def test_the_progress_shows_the_texts_and_the_validation(tmp_path, monkeypatch):
    result, session = _run(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert f"new {len(session.posts)}" in result.output
    assert "Validando EPUB 3/3" in result.output


def test_a_failure_shows_its_text_only_to_a_developer(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    session = Session(failing="Goodbye reader.")
    developer, _ = _run(tmp_path, monkeypatch, "--fail-fast", source=source, session=session)
    assert "HTTP error 500" in " ".join(developer.output.split())

    arguments = _args(tmp_path, source, "--fail-fast")
    arguments[1] = "common"
    common, _ = _run(tmp_path, monkeypatch, mode_args=arguments, session=Session(failing="Goodbye reader."))
    assert common.exit_code == 1 and "--fail-fast" in common.output and "HTTP error 500" not in " ".join(common.output.split())


def test_overwrite_is_part_of_the_plan(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    _run(tmp_path, monkeypatch, source=source)
    _run(tmp_path, monkeypatch, "--overwrite", source=source)

    assert len({job.request.input_fingerprint for job in _jobs(tmp_path)}) == 2


def test_a_preflight_failure_has_no_report_even_on_a_book_that_cannot_be_read(tmp_path):
    broken = tmp_path / "book.epub"
    broken.write_bytes(b"not an epub")
    projects = ProjectJsonRepository(tmp_path / "processing")
    project = projects.resolve(build_project_request(broken, source="en", target="pt")).project
    error = PreflightError("no", "no")

    def prepare():
        raise error

    run = composition.translate_in_project(
        composition.ProjectTranslation(tmp_path / "processing", project.project_id, project.source_sha256, broken,
                                       TranslationOptions(language_pair=LanguagePair(source="en", target="pt")),
                                       OutputPlan(path=tmp_path / "out.epub"), False, prepare, lambda: None),
        TranslateObserver())

    assert run.report is None and run.preflight_error is error
    assert run.result.state is JobState.FAILED and run.result.failure.code == "preflight_failed"


def test_dry_run_creates_no_job_and_no_project(tmp_path, monkeypatch):
    result, session = _run(tmp_path, monkeypatch, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not session.posts and not (tmp_path / "processing" / "projects").exists()


def test_choosing_to_overwrite_at_the_prompt_replaces_the_file(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    output = tmp_path / "out.epub"
    output.write_bytes(b"old")
    arguments = _args(tmp_path, source)
    arguments[1] = "common"
    result, _ = _run(tmp_path, monkeypatch, mode_args=arguments, stdin="1\nn\n")

    assert result.exit_code == 0, result.output
    assert output.read_bytes()[:2] == b"PK"


def test_a_file_that_appears_during_the_run_is_left_alone(tmp_path, monkeypatch):
    output = tmp_path / "out.epub"

    def appear(_text: str) -> None:
        output.write_bytes(b"someone else's")

    result, _ = _run(tmp_path, monkeypatch, session=Session(on_post=appear))

    assert result.exit_code == 1
    assert output.read_bytes() == b"someone else's"
    assert "--overwrite" in result.output and "não foi alterado" in " ".join(result.output.split())
    assert not _leftovers(tmp_path)
    job = _only_job(tmp_path)
    assert (job.state.value, job.terminal_summary.reason_code) == ("failed", "output_exists")


@pytest.mark.parametrize("arguments", [(), ("--dry-run",)], ids=["translate", "dry_run"])
def test_a_protected_book_is_refused_before_anything_starts(tmp_path, monkeypatch, arguments):
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))
    before = source.read_bytes()
    result, session = _run(tmp_path, monkeypatch, *arguments, source=source)

    assert result.exit_code == 1
    assert "protegido" in result.output and "encrypted_content" in result.output
    assert not session.posts and not (tmp_path / "out.epub").exists() and source.read_bytes() == before
    assert not ResumeStateStore(tmp_path / "processing").scan().running


def test_a_protected_book_records_why_its_job_failed(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))
    _run(tmp_path, monkeypatch, source=source)

    job = _only_job(tmp_path)
    assert (job.state.value, job.terminal_summary.reason_code) == ("failed", "encrypted_content")


def test_a_protected_book_is_refused_by_the_preview_too(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))
    result, session = _run(tmp_path, monkeypatch, mode_args=["--mode", "developer", "--preview", str(source)], source=source)

    assert result.exit_code == 1 and "protegido" in result.output and not session.posts


def test_the_resume_command_cannot_get_a_protected_book_through(tmp_path, monkeypatch):
    plain = _book(tmp_path / "book.epub")
    interrupted, _ = _run(tmp_path, monkeypatch, source=plain, session=Session(on_post=_interrupt_on("Goodbye")))
    assert interrupted.exit_code == 1
    _book(plain, extra=(("META-INF/encryption.xml", ENCRYPTED_DOCUMENT),))

    session = Session()
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: session)
    result = runner.invoke(app, ["--mode", "developer", "resume", str(plain), "--target", "pt"])

    assert result.exit_code == 1 and "protegido" in result.output and not session.posts


def _interrupt_on(fragment: str):
    def interrupt(text: str) -> None:
        if fragment in text:
            raise KeyboardInterrupt

    return interrupt


def test_a_font_with_the_standard_obfuscation_is_not_a_protected_book(tmp_path, monkeypatch):
    obfuscated = ENCRYPTED_DOCUMENT.replace("http://www.w3.org/2001/04/xmlenc#aes256-cbc", "http://www.idpf.org/2008/embedding").replace("OEBPS/c1.xhtml", "OEBPS/f.otf")
    source = _book(tmp_path / "book.epub", extra=(("META-INF/encryption.xml", obfuscated), ("OEBPS/f.otf", "font")))
    result, _ = _run(tmp_path, monkeypatch, source=source)

    assert result.exit_code == 0, result.output


def test_a_lost_lease_publishes_nothing_and_the_retry_needs_no_translator(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    real_open = composition.open_translate_project

    def open_with_clock(*args, **kwargs):
        return real_open(*args, clock=lambda: now[0], lease_renewal=timedelta(days=1), **kwargs)

    def sleep_for_two_hours(_text: str) -> None:
        now[0] = datetime(2026, 1, 1, 2, tzinfo=UTC)

    monkeypatch.setattr(composition, "open_translate_project", open_with_clock)
    result, session = _run(tmp_path, monkeypatch, session=Session(on_post=sleep_for_two_hours))

    assert result.exit_code == 1
    assert "suspenso" in result.output and "lease_expired" in result.output
    assert not (tmp_path / "out.epub").exists() and not _leftovers(tmp_path)
    translated = list(session.posts)
    assert translated

    retry = Session()
    again, _ = _run(tmp_path, monkeypatch, session=retry, source=tmp_path / "book.epub")
    assert again.exit_code == 0, again.output
    assert not retry.posts
    assert (tmp_path / "out.epub").exists()


CHILD = textwrap.dedent('''\
    import json, os, sys, time
    import requests
    from typer.testing import CliRunner
    import ayvu.translator
    from ayvu import cli
    class Session:
        def _response(self, payload):
            response = requests.Response()
            response.status_code = 200
            response.url = "http://translator.test"
            response._content = json.dumps(payload).encode()
            return response
        def get(self, url, *, timeout):
            return self._response([{"code": "en", "name": "E", "targets": ["pt"]}, {"code": "pt", "name": "P", "targets": []}])
        def post(self, url, *, json, timeout):
            if "Goodbye" in json["q"]:
                os.write(1, b"blocked\\n")
                while True:
                    time.sleep(60)
            return self._response({"translatedText": "PT:" + json["q"]})
        def close(self):
            pass
    ayvu.translator.requests.Session = Session
    processing = sys.argv[1]
    cli._processing_dir = lambda _config: __import__("pathlib").Path(processing)
    CliRunner().invoke(cli.app, sys.argv[2:])
''')


def test_a_killed_run_blocks_nothing_and_a_live_one_blocks_its_duplicate(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    arguments = _args(tmp_path, source)
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "processing"), *arguments], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment, cwd=tmp_path)
    try:
        assert child.stdout.readline() == b"blocked\n"
        duplicate, session = _run(tmp_path, monkeypatch, source=source)
        assert duplicate.exit_code == 1 and "em andamento" in duplicate.output and not session.posts
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
        child.stdout.close()

    retry, _ = _run(tmp_path, monkeypatch, source=source)

    assert retry.exit_code == 0, retry.output
    assert (tmp_path / "out.epub").exists() and not _leftovers(tmp_path)
    states = sorted(job.state.value for job in _jobs(tmp_path))
    assert states == ["failed", "validated"]


def test_a_full_run_stores_no_text_no_address_and_no_translation_in_the_job_store(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output

    databases = list((tmp_path / "processing" / "projects").rglob("*.sqlite*")) + list((tmp_path / "processing" / "projects").rglob("*.db"))
    assert databases
    raw = b"".join(path.read_bytes() for path in databases)
    for canary in (b"Hello title", b"PT:Hello", b"translator.test", str(tmp_path / "out.epub").encode()):
        assert canary not in raw

    job = _only_job(tmp_path)
    project_store = ProjectJsonRepository(tmp_path / "processing")
    paths = project_store.resolve_database_paths(job.request.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, job.request.project_id,
                                            clock=lambda: datetime.now(UTC))
    records = provenance.list_by_job(job.job_id, limit=1024).nodes
    destination_records = [node.record.request for node in records if node.record.request.operation == "translate_output"]
    assert len(destination_records) == 1
    assert destination_records[0].subject is not None
    assert destination_records[0].subject.namespace == "artifact_destination"
    assert str(tmp_path / "out.epub") not in destination_records[0].subject.value


def _canned(tmp_path, monkeypatch, result, report=None):
    output = tmp_path / "out.epub"
    run = ProjectTranslationRun(result, report(output) if report else None, None)
    monkeypatch.setattr("ayvu.cli.translate_in_project", lambda _translation, _observer: run)
    invocation, _ = _run(tmp_path, monkeypatch)
    return invocation


def _report(output: Path) -> TranslationReport:
    return TranslationReport(output_path=output, input_path=output.with_name("book.epub"), target_language="pt")


def _result(state, reason, **more) -> TranslateProjectResult:
    return TranslateProjectResult("project", "job", state, JobOutcome(state, JobTerminalSummary(reason)), **more)


def test_a_lost_lineage_warns_and_keeps_the_exit_code(tmp_path, monkeypatch):
    result = _result(JobState.PARTIALLY_COMPLETED, "provenance_failed", validation=OutputValidation(True, (), 1))
    invocation = _canned(tmp_path, monkeypatch, result, _report)

    assert invocation.exit_code == 0, invocation.output
    assert "histórico" in invocation.output and "Translation report" in invocation.output


def test_a_job_that_could_not_be_recorded_is_not_called_a_success(tmp_path, monkeypatch):
    result = TranslateProjectResult("project", "job", JobState.VALIDATING, validation=OutputValidation(True, (), 1), recorded=False)
    invocation = _canned(tmp_path, monkeypatch, result, _report)

    assert invocation.exit_code == 1
    assert "registrar o fim" in invocation.output


def test_a_publication_that_could_not_be_recorded_says_where_the_book_is(tmp_path, monkeypatch):
    failure = JobFailure(JobFailureFamily.RESOURCE, "artifact_store_failed")
    summary = JobTerminalSummary("publication_uncommitted")
    result = TranslateProjectResult("project", "job", JobState.PARTIALLY_COMPLETED, JobOutcome(JobState.PARTIALLY_COMPLETED, summary), failure)
    invocation = _canned(tmp_path, monkeypatch, result)

    assert invocation.exit_code == 1
    assert "foi publicado" in invocation.output and "out.epub" in invocation.output and "Translation report" not in invocation.output


def test_a_refusal_with_no_job_gets_its_message(tmp_path, monkeypatch):
    refused = TranslateProjectResult("project", failure=JobFailure(JobFailureFamily.POLICY, "job_in_progress"))
    invocation = _canned(tmp_path, monkeypatch, refused)

    assert invocation.exit_code == 1 and "em andamento" in invocation.output


@pytest.mark.parametrize("error", [JobError("job_store_failed"), OSError("/private/secret path"), sqlite3.OperationalError("disk I/O /private/secret")],
                         ids=["job", "os", "sqlite"])
def test_a_project_store_that_will_not_open_stops_before_the_translator(tmp_path, monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(composition, "open_job_repository", fail)
    invocation, session = _run(tmp_path, monkeypatch)

    assert invocation.exit_code == 1 and isinstance(invocation.exception, SystemExit)
    assert "estado do projeto" in invocation.output and not session.posts and "secret" not in invocation.output
    assert not (tmp_path / "out.epub").exists()


def test_an_unreadable_book_keeps_the_current_message_on_every_path(tmp_path, monkeypatch):
    broken = tmp_path / "book.epub"
    broken.write_bytes(b"not an epub")
    for arguments in ((), ("--dry-run",)):
        invocation, session = _run(tmp_path, monkeypatch, *arguments, source=broken)
        assert invocation.exit_code == 1 and "Não foi possível ler o EPUB" in invocation.output and not session.posts


@pytest.mark.parametrize("code", sorted(_TRANSLATION_FAILURES))
def test_every_failure_has_an_answer_and_quotes_only_the_output(code, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli_module.console, "width", 400, raising=False)
    _print_translation_failure(_BookOutcome(failure=code), tmp_path / "out.epub", UserMode.COMMON)

    written = capsys.readouterr().out
    assert written.strip() and "{" not in written and code not in written


def test_a_failure_nobody_named_still_gets_an_answer(tmp_path, capsys):
    _print_translation_failure(_BookOutcome(failure="never_heard_of_it", raised=ValueError("secret text")), tmp_path / "out.epub", UserMode.COMMON)

    written = capsys.readouterr().out
    assert "Não foi possível concluir" in written and "secret text" not in written


def test_every_book_refusal_has_its_own_answer():
    assert composition.BOOK_REFUSALS <= set(_TRANSLATION_FAILURES)
