from __future__ import annotations
from zipfile import ZipFile
from ayvu.composition.builders import translate_project as composition
from ayvu.translation.preflight import PreflightError
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.recovery.checkpoint import ResumeStateStore
from ayvu.jobs.model import JobState
from ayvu.translation.project_contracts import TranslateObserver
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from tests.support.cli_translate_project_support import Session, _args
from tests.support.cli_translate_project_support import _book, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _leftovers, _only_job
from tests.support.cli_translate_project_support import _run


def test_a_translation_ends_as_a_validated_job_with_the_output_in_place(tmp_path, monkeypatch):
    result, session = _run(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    job = _only_job(tmp_path)
    assert job.state.value == "validated" and job.terminal_summary.reason_code == "validated"
    assert dict(job.terminal_summary.counters)["translated"] == len(session.posts) > 0
    with ZipFile(tmp_path / "out.epub") as archive:
        assert b"PT:Hello title" in archive.read("OEBPS/c1.xhtml")
    assert not _leftovers(tmp_path)


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
