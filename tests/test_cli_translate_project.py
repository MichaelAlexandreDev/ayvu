"""The real ``translate`` command through the use case, watched from the job record and the files it leaves."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest
import requests
from typer.testing import CliRunner

import ayvu.cli as cli_module
import ayvu.translate_project_composition as composition
from ayvu.cli import _TRANSLATION_FAILURES, _BookOutcome, _print_translation_failure, app
from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions, UserMode
from ayvu.project_schema import open_job_repository
from ayvu.preflight import PreflightError
from ayvu.project_store import ProjectJsonRepository, build_project_request
from ayvu.resume import ResumeStateStore
from ayvu.epub_io import TranslationReport
from ayvu.job import JobError, JobState, JobTerminalSummary
from ayvu.job_events import JobFailure, JobFailureFamily, JobOutcome
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


def _canned(tmp_path, monkeypatch, result, report=None):
    output = tmp_path / "out.epub"
    run = ProjectTranslationRun(result, report(output) if report else None, [], None)
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
