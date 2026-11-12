import json
from typer.testing import CliRunner
from ayvu.interfaces.cli import app as cli, report as cli_report
import ayvu.composition.cli as composition
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _write_epub
from tests.interfaces.cli.translate_batch_integration_support import _run, isolated_cli


def test_real_validation_warning_keeps_artifact_and_continues_batch(tmp_path, monkeypatch):
    from tests.support.cli_translate_project_support import _jobs

    first = _write_epub(tmp_path / "first.epub", chapter_two='<p><a href="gone.xhtml">Broken link</a></p>')
    second = _write_epub(tmp_path / "second.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--continue-on-error", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 1, result.output
    assert "gone.xhtml" in result.output
    assert (output / "first-pt.epub").is_file()
    assert (output / "second-pt.epub").is_file()
    jobs = _jobs(tmp_path)
    assert len(jobs) == 2
    assert any(job.state.value == "partially_completed" and job.terminal_summary.reason_code == "validation_warnings" for job in jobs)


def test_real_cache_only_batch_writes_outputs_and_missing_reports_without_provider_calls(tmp_path, monkeypatch):
    from tests.support.workflow_characterization_support import _read_member

    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"
    original = [first.read_bytes(), second.read_bytes()]

    result = _run(first, second, output, "--cache-only", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 0, result.output
    assert not session.posts
    assert "Hello title" in _read_member(output / "first-pt.epub", "OEBPS/text/chapter1.xhtml")
    assert "PT:Hello title" not in _read_member(output / "first-pt.epub", "OEBPS/text/chapter1.xhtml")
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    for name in ("first", "second"):
        assert (reports / f"{name}-pt-report.md").is_file()
        assert "Hello title" in (reports / f"{name}-pt-missing.txt").read_text()
    assert [first.read_bytes(), second.read_bytes()] == original


def test_interrupted_active_batch_item_can_resume_without_starting_next(tmp_path, monkeypatch):
    from ayvu.v1.resume import ResumeStateStore

    first = _write_epub(tmp_path / "first.epub", chapter_one="<p>First chapter text.</p>",
                        chapter_two='<p id="two">Interrupt when translating this sentence.</p>')
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    session = FakeLibreTranslateSession(_direct_languages(), interrupt_on_text="Interrupt when translating")
    processing = _install_cli_environment(monkeypatch, tmp_path, session)

    interrupted = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                       "--url", "http://localhost:5000", "--retries", "0")

    assert interrupted.exit_code == 1
    assert "Batch item 2/2" not in interrupted.output
    assert not (output / "first-pt.epub").exists()
    running = ResumeStateStore(processing).scan().running
    assert len(running) == 1 and running[0].completed_chapters
    resumed_session = FakeLibreTranslateSession(_direct_languages())
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: resumed_session)
    resumed = CliRunner().invoke(cli.app, ["--mode", "developer", "resume", str(first), "--target", "pt"])
    assert resumed.exit_code == 0, resumed.output
    assert (output / "first-pt.epub").is_file()
    assert not (output / "second-pt.epub").exists()
    assert "First chapter text." not in "\n".join(p["q"] for p in resumed_session.posts)


def test_batch_workers_bound_provider_concurrency_and_do_not_overlap_books(tmp_path, monkeypatch):
    import threading

    class InstrumentedSession(FakeLibreTranslateSession):
        def __init__(self):
            super().__init__(_direct_languages())
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.seen = []
            self.barriers = {name: threading.Barrier(2) for name in ("First book", "Second book")}

        def post(self, url, *, json, timeout, allow_redirects=False):
            assert allow_redirects is False
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.seen.append((json["q"], self.active))
            try:
                for name, barrier in self.barriers.items():
                    if name in json["q"]:
                        barrier.wait(timeout=5)
                        break
                return super().post(url, json=json, timeout=timeout, allow_redirects=allow_redirects)
            finally:
                with self.lock:
                    self.active -= 1

    first = _write_epub(tmp_path / "first.epub", chapter_one="<p>First book chapter one.</p>",
                        chapter_two='<p id="two">First book chapter two.</p>')
    second = _write_epub(tmp_path / "second.epub", chapter_one="<p>Second book chapter one.</p>",
                         chapter_two='<p id="two">Second book chapter two.</p>')
    session = InstrumentedSession()
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = _run(first, second, tmp_path / "out", "--workers", "2", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 0, result.output
    assert session.maximum == 2
    texts = [text for text, _ in session.seen]
    first_positions = [i for i, text in enumerate(texts) if "First book" in text]
    second_positions = [i for i, text in enumerate(texts) if "Second book" in text]
    assert first_positions and second_positions
    assert max(first_positions) < min(second_positions)


def test_real_failed_text_is_partial_and_batch_continues(tmp_path, monkeypatch):
    from tests.support.cli_translate_project_support import _jobs
    from tests.support.workflow_characterization_support import FailingSession, _read_member

    first = _write_epub(tmp_path / "first.epub", chapter_two='<p id="two">Fail only this text.</p>')
    second = _write_epub(tmp_path / "second.epub")
    session = FailingSession(_direct_languages(), "Fail only this text.")
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 0, result.output
    assert "Fail only this text." in _read_member(output / "first-pt.epub", "OEBPS/text/chapter2.xhtml")
    assert (output / "second-pt.epub").is_file()
    assert any(job.state.value == "partially_completed" and job.terminal_summary.reason_code == "texts_failed"
               for job in _jobs(tmp_path))


def test_report_failure_after_publication_keeps_durable_facts_and_continues(tmp_path, monkeypatch):
    import sqlite3

    from ayvu.projects.store import ProjectJsonRepository
    from ayvu.translation.batch import ItemStatus
    from tests.support.cli_translate_project_support import _jobs

    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    session = FakeLibreTranslateSession(_direct_languages())
    processing = _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"
    captured = []
    original_factory = composition.LocalTranslateBatch
    original_save = cli_report._save_markdown_report
    save_calls = []

    def fail_first_report(*args, **kwargs):
        save_calls.append(args[0].input_path)
        if len(save_calls) == 1:
            raise OSError("simulated report write failure")
        return original_save(*args, **kwargs)

    def capture_factory(*args, **kwargs):
        service = original_factory(*args, **kwargs)
        execute = service.use_case.execute
        def capture(command):
            result = execute(command)
            captured.append(result)
            return result
        service.use_case.execute = capture
        return service

    monkeypatch.setattr(cli_report, "_save_markdown_report", fail_first_report)
    monkeypatch.setattr(composition, "LocalTranslateBatch", capture_factory)
    result = _run(first, second, output, "--continue-on-error", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://localhost:5000", "--retries", "0")

    assert result.exit_code == 1, result.output
    assert save_calls == [first, second], result.output
    assert "O EPUB foi publicado, mas o relatório não pôde ser salvo." in result.output
    assert len(captured) == 1 and not captured[0].succeeded
    failed, succeeded = [item.outcome for item in captured[0].items]
    assert failed.status is ItemStatus.FAILED and failed.failure == "report_failed"
    assert failed.job_ref and failed.job_state == "validated" and failed.output_published
    assert failed.artifact_ref is None and failed.report_ref is None
    assert succeeded.status is ItemStatus.SUCCESS and succeeded.output_published
    assert succeeded.job_ref and succeeded.artifact_ref and succeeded.report_ref
    jobs = {job.job_id: job for job in _jobs(tmp_path)}
    assert jobs[failed.job_ref].state.value == jobs[succeeded.job_ref].state.value == "validated"
    ready = set()
    projects = ProjectJsonRepository(processing)
    for path in processing.glob("projects/*/project.json"):
        project_id = json.loads(path.read_text())["project_id"]
        database = projects.resolve_database_paths(project_id).database
        with sqlite3.connect(database) as connection:
            ready.update(job_id for job_id, state in connection.execute("SELECT job_id, state FROM job_artifacts")
                         if state == "ready")
    assert {failed.job_ref, succeeded.job_ref} <= ready
    assert all((output / f"{path.stem}-pt.epub").read_bytes().startswith(b"PK") for path in (first, second))
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert not (reports / "first-pt-report.md").exists()
    assert (reports / "second-pt-report.md").is_file()
    assert [first.read_bytes(), second.read_bytes()] == originals
