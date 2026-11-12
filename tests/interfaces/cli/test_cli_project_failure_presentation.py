from __future__ import annotations
import sqlite3
from datetime import UTC, datetime
import pytest
from ayvu.interfaces.cli import render as cli_module
from ayvu.composition.builders import translate_project as composition
from ayvu.composition.cli import _BookOutcome
from ayvu.interfaces.cli.run_messages import _print_translation_failure
from ayvu.interfaces.cli.render import _TRANSLATION_FAILURES
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository
from ayvu.jobs.model import JobState, JobTerminalSummary
from ayvu.jobs.events import JobOutcome
from ayvu.translation.project_contracts import OutputValidation, TranslateProjectResult
from ayvu.kernel import UserMode, JobError, JobFailure, JobFailureFamily
from tests.support.cli_translate_project_support import _canned, _isolated_from_the_user
from tests.support.cli_translate_project_support import _only_job, _report, _result
from tests.support.cli_translate_project_support import _run


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
