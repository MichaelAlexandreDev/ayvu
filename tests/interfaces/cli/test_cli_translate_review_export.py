import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.interfaces.cli.app import app
from ayvu.composition.cli import _BookOutcome
from ayvu.v1.epub import TranslationReport
from ayvu.jobs.model import JobState
from ayvu.review.application import ReviewExportResult, ReviewOutcome, ReviewPackageIdentity
from ayvu.translation.engine import EngineResult
from ayvu.translation.project_contracts import OutputValidation, TranslateProjectResult
from tests.support.cli_support import isolated_config, runner


def test_translate_command_delegates_v2_review_export_from_exact_project_result(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    review_path = tmp_path / "book-review.csv"
    processing_dir = tmp_path / "processing"
    epub_path.write_bytes(b"fake epub")
    project_id = "00000000-0000-4000-8000-000000000148"
    job_id = "00000000-0000-4000-8000-000000000149"
    machine_id = "00000000-0000-4000-8000-000000000150"
    review_job_id = "00000000-0000-4000-8000-000000000151"
    review_artifact_id = "00000000-0000-4000-8000-000000000152"
    now = datetime(2026, 11, 5, tzinfo=UTC)
    digest = "sha256:" + "a" * 64
    machine = Artifact(machine_id, ArtifactRequest(project_id, job_id, "translated_output", "reconstruct", 1,
                                                   digest, "application/epub+zip"),
                       ArtifactState.READY, 2, now, now, 10, digest)
    translation = TranslateProjectResult(
        project_id, job_id, JobState.VALIDATED, artifact=machine,
        engine=EngineResult(documents=(object(),), translations=(object(),)),
        artifact_destination=output_path, project_revision=1)
    report = TranslationReport(output_path=output_path, input_path=epub_path, output_written=True,
                               detected_language="en", target_language="pt")
    monkeypatch.setattr("ayvu.interfaces.cli.settings._processing_dir", lambda _config: processing_dir)
    monkeypatch.setattr("ayvu.composition.cli.build_project_request", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("ayvu.composition.cli.resolve_project", lambda *_args, **_kwargs: SimpleNamespace(
        project=SimpleNamespace(project_id=project_id, source_sha256="b" * 64)))
    monkeypatch.setattr("ayvu.interfaces.cli.run_support._translate_in_project", lambda *_args, **_kwargs:
                        _BookOutcome(report=report, validation=OutputValidation(True, (), 1), job_result=translation))

    package_bytes = b"AYVU-REVIEW-PACKAGE,2,{}\r\n"
    package_fingerprint = "sha256:" + hashlib.sha256(package_bytes).hexdigest()
    identity = ReviewPackageIdentity(2, "rpk:v1:" + "c" * 64, package_fingerprint, project_id, 1,
                                     "b" * 64, 10, machine_id, 2, digest, 1, 1)
    review_artifact = Artifact(
        review_artifact_id, ArtifactRequest(project_id, review_job_id, "review_package", "review_export", 1,
                                           digest, "text/csv"), ArtifactState.READY, 2, now, now,
        len(package_bytes), package_fingerprint)
    captured = {}

    class FakeExport:
        def execute(self, command):
            captured["command"] = command
            review_path.write_bytes(package_bytes)
            return ReviewExportResult(ReviewOutcome.READY, "review_package_exported", identity,
                                      review_artifact, review_job_id)

    class FakeUseCases:
        export = FakeExport()

        @staticmethod
        def cancellation():
            return SimpleNamespace(token=None)

    monkeypatch.setattr("ayvu.interfaces.cli.run.open_review_use_cases", lambda *_args, **_kwargs: FakeUseCases())
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 0
    assert "Review file saved to:" in result.output
    assert "Review file" in result.output
    assert review_path.read_bytes().startswith(b"AYVU-REVIEW-PACKAGE,2,")
    assert captured["command"].translation is translation
    assert captured["command"].destination == review_path
    assert captured["command"].overwrite is False


def test_translate_command_rejects_existing_review_output_without_overwrite(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    review_path = tmp_path / "book-review.csv"
    epub_path.write_bytes(b"fake epub")
    review_path.write_text("already here", encoding="utf-8")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run when review output already exists")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 1
    assert "Arquivo de revisão já existe." in result.output
    assert "Use --overwrite" in result.output
    assert str(review_path) not in result.output
    assert review_path.read_text(encoding="utf-8") == "already here"
    assert "Traceback" not in result.output


def test_translate_command_rejects_review_output_with_dry_run(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    review_path = tmp_path / "book-review.csv"
    epub_path.write_bytes(b"fake epub")

    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run for dry-run review output")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--dry-run",
            "--review-output",
            str(review_path),
        ],
    )

    assert result.exit_code == 1
    assert "Não é possível gerar arquivo de revisão em dry-run." in result.output
    assert not review_path.exists()
    assert "Traceback" not in result.output
