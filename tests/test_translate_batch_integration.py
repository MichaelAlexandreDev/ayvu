"""Integration checks for complete batch planning and dry-run publications."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ayvu.cli as cli
from test_workflow_characterization import (
    FakeLibreTranslateSession,
    _direct_languages,
    _install_cli_environment,
    _write_epub,
)


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


def _run(first, second, output, *extra):
    return CliRunner().invoke(cli.app, [
        "--mode", "developer", "translate", str(first), str(second),
        "--output-dir", str(output), *extra,
    ])


def test_final_conflict_rejects_before_first_item(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    output = tmp_path / "output"
    output.mkdir()
    conflict = output / "second-pt.epub"
    conflict.write_bytes(b"existing")
    calls = []
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, output)

    assert result.exit_code == 1
    assert "existing" in result.output.lower()
    assert calls == []
    assert conflict.read_bytes() == b"existing"
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert not (output / "first-pt.epub").exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_same_stem_destinations_rejected_before_first_item(tmp_path, monkeypatch, overwrite):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    first = _write_epub(left / "book.epub")
    second = _write_epub(right / "book.epub", language="fr")
    calls = []
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: calls.append(kw))
    result = _run(first, second, tmp_path / "out", *( ["--overwrite"] if overwrite else [] ))

    assert result.exit_code == 1
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_dry_run_existing_output_saves_each_report_without_replacing_epub(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub", language="en")
    second = _write_epub(tmp_path / "second.epub", language="fr")
    originals = [first.read_bytes(), second.read_bytes()]
    output = tmp_path / "out"
    output.mkdir()
    existing = output / "second-pt.epub"
    existing.write_bytes(b"existing")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = _run(first, second, output, "--dry-run")

    assert result.exit_code == 0, result.output
    assert existing.read_bytes() == b"existing"
    assert not (output / "first-pt.epub").exists()
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert (reports / "first-pt-report.md").is_file()
    assert (reports / "second-pt-report.md").is_file()
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert not session.posts


def test_output_cannot_replace_another_source_even_with_overwrite(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "book.epub")
    second = _write_epub(tmp_path / "book-pt.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    calls = []
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, tmp_path, "--overwrite")

    assert result.exit_code == 1
    assert calls == []
    assert [first.read_bytes(), second.read_bytes()] == originals


@pytest.mark.parametrize("alias", ["hardlink", "symlink"])
def test_existing_output_alias_to_any_source_rejected(tmp_path, monkeypatch, alias):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    destination = output / "first-pt.epub"
    if alias == "hardlink":
        destination.hardlink_to(second)
    else:
        destination.symlink_to(second)
    before = [first.read_bytes(), second.read_bytes()]
    calls = []
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: calls.append(kw))

    result = _run(first, second, output, "--overwrite")

    assert result.exit_code == 1
    assert calls == []
    assert [first.read_bytes(), second.read_bytes()] == before
    assert destination.read_bytes() == before[1]


def test_common_mode_cancel_conflict_on_final_item_starts_nothing(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    conflict = output / "second-pt.epub"
    conflict.write_bytes(b"existing")
    calls = []
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: calls.append(kw))

    result = CliRunner().invoke(cli.app, [
        "--mode", "common", "translate", str(first), str(second),
        "--output-dir", str(output),
    ], input="0\n")

    assert result.exit_code == 1
    assert "Choose an option" in result.output
    assert calls == []
    assert conflict.read_bytes() == b"existing"


def test_cache_only_dry_run_keeps_reports_and_automatic_missing_files(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = _run(first, second, output, "--dry-run", "--cache-only", "--cache", str(tmp_path / "cache.sqlite"))

    assert result.exit_code == 0, result.output
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert sorted(p.name for p in reports.glob("*.md")) == ["first-pt-report.md", "second-pt-report.md"]
    assert sorted(p.name for p in reports.glob("*.txt")) == ["first-pt-missing.txt", "second-pt-missing.txt"]
    assert not list(output.glob("*.epub"))
    assert not session.posts


def test_automatic_reports_and_missing_files_follow_the_umask_like_any_other_output(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    _install_cli_environment(monkeypatch, tmp_path, FakeLibreTranslateSession(_direct_languages()))
    previous = os.umask(0o027)
    try:
        result = _run(first, second, tmp_path / "out", "--dry-run", "--cache-only", "--cache", str(tmp_path / "cache.sqlite"))
    finally:
        os.umask(previous)

    assert result.exit_code == 0, result.output
    reports = tmp_path / "home" / "Documentos" / "Livros" / "Relatorios"
    assert {item.stat().st_mode & 0o777 for item in reports.iterdir()} == {0o640}


def test_real_project_batch_resolves_each_source_language(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub", language="en")
    second = _write_epub(tmp_path / "second.epub", language="fr")
    originals = [first.read_bytes(), second.read_bytes()]
    languages = _direct_languages() + [{"code": "fr", "name": "French", "targets": ["pt"]}]
    session = FakeLibreTranslateSession(languages)
    processing = _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://translator.test", "--retries", "0")

    assert result.exit_code == 0, result.output
    manifests = [json.loads(path.read_text()) for path in processing.glob("projects/*/project.json")]
    assert {p["source"] for p in manifests} == {"en", "fr"}
    assert len({p["project_id"] for p in manifests}) == 2
    assert {item["source"] for item in session.posts} == {"en", "fr"}
    assert [first.read_bytes(), second.read_bytes()] == originals
    assert sorted(p.name for p in output.glob("*.epub")) == ["first-pt.epub", "second-pt.epub"]


def test_common_rename_and_overwrite_are_decided_before_translation(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    old_first = output / "first-pt.epub"
    old_second = output / "second-pt.epub"
    old_first.write_bytes(b"first old")
    old_second.write_bytes(b"second old")
    renamed = output / "renamed.epub"
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)

    result = CliRunner().invoke(cli.app, [
        "--mode", "common", "translate", str(first), str(second), "--output-dir", str(output),
        "--source", "en", "--cache", str(tmp_path / "cache.sqlite"), "--url", "http://translator.test", "--retries", "0",
    ], input=f"2\n{renamed}\n1\n")

    assert result.output.count("Choose an option") == 2
    assert result.output.rfind("Choose an option") < result.output.index("Batch item 1/2")
    assert result.exit_code == 0, result.output
    assert old_first.read_bytes() == b"first old"
    assert old_second.read_bytes().startswith(b"PK")
    assert renamed.read_bytes().startswith(b"PK")
    assert session.posts


def test_common_refusal_has_no_empty_batch_summary(tmp_path, monkeypatch):
    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    output.mkdir()
    (output / "second-pt.epub").write_bytes(b"old")
    monkeypatch.setattr(cli, "_run_translation", lambda **kw: pytest.fail("item started"))

    result = CliRunner().invoke(cli.app, ["--mode", "common", "translate", str(first), str(second),
                                          "--output-dir", str(output)], input="0\n")

    assert result.exit_code == 1
    assert "Batch translation summary" not in result.output


def test_real_validation_warning_keeps_artifact_and_continues_batch(tmp_path, monkeypatch):
    from test_cli_translate_project import _jobs

    first = _write_epub(tmp_path / "first.epub", chapter_two='<p><a href="gone.xhtml">Broken link</a></p>')
    second = _write_epub(tmp_path / "second.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--continue-on-error", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://translator.test", "--retries", "0")

    assert result.exit_code == 1, result.output
    assert "gone.xhtml" in result.output
    assert (output / "first-pt.epub").is_file()
    assert (output / "second-pt.epub").is_file()
    jobs = _jobs(tmp_path)
    assert len(jobs) == 2
    assert any(job.state.value == "partially_completed" and job.terminal_summary.reason_code == "validation_warnings" for job in jobs)


def test_real_cache_only_batch_writes_outputs_and_missing_reports_without_provider_calls(tmp_path, monkeypatch):
    from test_workflow_characterization import _read_member

    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"
    original = [first.read_bytes(), second.read_bytes()]

    result = _run(first, second, output, "--cache-only", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://translator.test", "--retries", "0")

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
    from ayvu.resume import ResumeStateStore

    first = _write_epub(tmp_path / "first.epub", chapter_one="<p>First chapter text.</p>",
                        chapter_two='<p id="two">Interrupt when translating this sentence.</p>')
    second = _write_epub(tmp_path / "second.epub")
    output = tmp_path / "out"
    session = FakeLibreTranslateSession(_direct_languages(), interrupt_on_text="Interrupt when translating")
    processing = _install_cli_environment(monkeypatch, tmp_path, session)

    interrupted = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                       "--url", "http://translator.test", "--retries", "0")

    assert interrupted.exit_code == 1
    assert "Batch item 2/2" not in interrupted.output
    assert not (output / "first-pt.epub").exists()
    running = ResumeStateStore(processing).scan().running
    assert len(running) == 1 and running[0].completed_chapters
    resumed_session = FakeLibreTranslateSession(_direct_languages())
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: resumed_session)
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

        def post(self, url, *, json, timeout):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.seen.append((json["q"], self.active))
            try:
                for name, barrier in self.barriers.items():
                    if name in json["q"]:
                        barrier.wait(timeout=5)
                        break
                return super().post(url, json=json, timeout=timeout)
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
                  "--url", "http://translator.test", "--retries", "0")

    assert result.exit_code == 0, result.output
    assert session.maximum == 2
    texts = [text for text, _ in session.seen]
    first_positions = [i for i, text in enumerate(texts) if "First book" in text]
    second_positions = [i for i, text in enumerate(texts) if "Second book" in text]
    assert first_positions and second_positions
    assert max(first_positions) < min(second_positions)


def test_real_failed_text_is_partial_and_batch_continues(tmp_path, monkeypatch):
    from test_cli_translate_project import _jobs
    from test_workflow_characterization import FailingSession, _read_member

    first = _write_epub(tmp_path / "first.epub", chapter_two='<p id="two">Fail only this text.</p>')
    second = _write_epub(tmp_path / "second.epub")
    session = FailingSession(_direct_languages(), "Fail only this text.")
    _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"

    result = _run(first, second, output, "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://translator.test", "--retries", "0")

    assert result.exit_code == 0, result.output
    assert "Fail only this text." in _read_member(output / "first-pt.epub", "OEBPS/text/chapter2.xhtml")
    assert (output / "second-pt.epub").is_file()
    assert any(job.state.value == "partially_completed" and job.terminal_summary.reason_code == "texts_failed"
               for job in _jobs(tmp_path))


def test_report_failure_after_publication_keeps_durable_facts_and_continues(tmp_path, monkeypatch):
    import sqlite3

    from ayvu.project_store import ProjectJsonRepository
    from ayvu.translate_batch import ItemStatus
    from test_cli_translate_project import _jobs

    first = _write_epub(tmp_path / "first.epub")
    second = _write_epub(tmp_path / "second.epub")
    originals = [first.read_bytes(), second.read_bytes()]
    session = FakeLibreTranslateSession(_direct_languages())
    processing = _install_cli_environment(monkeypatch, tmp_path, session)
    output = tmp_path / "out"
    captured = []
    original_factory = cli.LocalTranslateBatch
    original_save = cli._save_markdown_report
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

    monkeypatch.setattr(cli, "_save_markdown_report", fail_first_report)
    monkeypatch.setattr(cli, "LocalTranslateBatch", capture_factory)
    result = _run(first, second, output, "--continue-on-error", "--cache", str(tmp_path / "cache.sqlite"),
                  "--url", "http://translator.test", "--retries", "0")

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
