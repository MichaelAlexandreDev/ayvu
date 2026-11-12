from __future__ import annotations
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
import pytest
from ayvu.composition.builders import translate_project as composition
from ayvu.interfaces.cli.app import app
from ayvu.v1.resume import ResumeStateStore
from tests.support.cli_translate_project_support import CHILD, ENCRYPTED_DOCUMENT, Session, _args
from tests.support.cli_translate_project_support import _book, _interrupt_on, _isolated_from_the_user, _jobs
from tests.support.cli_translate_project_support import _leftovers, _only_job
from tests.support.cli_translate_project_support import _run, runner


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
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    result = runner.invoke(app, ["--mode", "developer", "resume", str(plain), "--target", "pt"])

    assert result.exit_code == 1 and "protegido" in result.output and not session.posts


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
