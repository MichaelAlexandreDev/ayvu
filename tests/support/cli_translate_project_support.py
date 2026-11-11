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
import ayvu.artifacts.destination as artifact_destination
from ayvu.interfaces.cli import render as cli_module
import ayvu.jobs.store as job_store
from ayvu.composition.builders import review as review_composition, review_files, translate_project as composition
from ayvu.composition.builders.review_operations import LocalReviewOperations
from ayvu.interfaces.cli.app import app
from ayvu.composition.cli import _BookOutcome
from ayvu.interfaces.cli.run_messages import _print_translation_failure
from ayvu.interfaces.cli.render import _TRANSLATION_FAILURES
from ayvu.projects.schema import open_job_repository, open_provenance_repository
from ayvu.translation.preflight import PreflightError
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.v1.resume import ResumeStateStore
from ayvu.review import decode_review_package, encode_review_package
from ayvu.v1.epub import TranslationReport
from ayvu.jobs.model import JobState, JobTerminalSummary
from ayvu.jobs.events import JobOutcome
from ayvu.provenance.model import SourceKind
from ayvu.review.application import ReviewApplyCommand, ReviewOutcome
from ayvu.translation.project_contracts import TRANSLATE_COMMAND_TYPE, OutputValidation, TranslateObserver, TranslateProjectResult
from ayvu.composition.builders.translate_project import ProjectTranslationRun
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions, UserMode, JobError, JobFailure, JobFailureFamily

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
    monkeypatch.setattr("ayvu.interfaces.cli.settings._processing_dir", lambda _config: tmp_path / "processing")


class Session:
    """An in-process LibreTranslate. ``on_post`` runs before each answer, and may raise or change the world."""

    def mount(self, prefix, adapter):
        assert prefix == "http://"

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

    def get(self, _url: str, *, timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
        return self._response(LANGUAGES)

    def post(self, _url: str, *, json: dict[str, str], timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
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
    return ["--mode", "developer", "translate", str(source), "--source", "en", "--target", "pt", "--url", "http://localhost:5000",
            "--retries", "0", "--cache", str(tmp_path / "cache.sqlite"), "--output", str(output or tmp_path / "out.epub"), *more]


def _run(tmp_path, monkeypatch, *more, source=None, session=None, output=None, mode_args=None, stdin=None):
    session = session or Session()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
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


def _translated_with_a_package(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    source = _book(tmp_path / "book.epub")
    review_path = tmp_path / "review.csv"
    translated, _ = _run(tmp_path, monkeypatch, "--review-output", str(review_path),
                         source=source, output=tmp_path / "machine.epub")
    assert translated.exit_code == 0, translated.output
    return source, review_path, tmp_path / "reviewed.epub"


def _interrupt_on(fragment: str):
    def interrupt(text: str) -> None:
        if fragment in text:
            raise KeyboardInterrupt

    return interrupt


CHILD = textwrap.dedent('''\
    import json, os, sys, time
    import requests
    from typer.testing import CliRunner
    import ayvu.providers.libretranslate.client
    from ayvu.interfaces.cli import app as cli, settings as cli_settings
    class Session:
        def mount(self, prefix, adapter):
            assert prefix == "http://"
        def _response(self, payload):
            response = requests.Response()
            response.status_code = 200
            response.url = "http://translator.test"
            response._content = json.dumps(payload).encode()
            return response
        def get(self, url, *, timeout, allow_redirects=False):
            assert allow_redirects is False
            return self._response([{"code": "en", "name": "E", "targets": ["pt"]}, {"code": "pt", "name": "P", "targets": []}])
        def post(self, url, *, json, timeout, allow_redirects=False):
            assert allow_redirects is False
            if "Goodbye" in json["q"]:
                os.write(1, b"blocked\\n")
                while True:
                    time.sleep(60)
            return self._response({"translatedText": "PT:" + json["q"]})
        def close(self):
            pass
    ayvu.providers.libretranslate.client.requests.Session = Session
    processing = sys.argv[1]
    cli_settings._processing_dir = lambda _config: __import__("pathlib").Path(processing)
    CliRunner().invoke(cli.app, sys.argv[2:])
''')


def _canned(tmp_path, monkeypatch, result, report=None):
    output = tmp_path / "out.epub"
    run = ProjectTranslationRun(result, report(output) if report else None, None)
    monkeypatch.setattr("ayvu.composition.builders.translate_project.translate_in_project", lambda _translation, _observer: run)
    invocation, _ = _run(tmp_path, monkeypatch)
    return invocation


def _report(output: Path) -> TranslationReport:
    return TranslationReport(output_path=output, input_path=output.with_name("book.epub"), target_language="pt")


def _result(state, reason, **more) -> TranslateProjectResult:
    return TranslateProjectResult("project", "job", state, JobOutcome(state, JobTerminalSummary(reason)), **more)
