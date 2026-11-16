"""The labeled surfaces the step 3 matrix still lacked: guided translation, its Markdown report and a failing online batch."""
from __future__ import annotations

import pytest

from ayvu.interfaces.cli import app as cli
from ayvu.jobs import JobState
from tests.support.cli_rich_runtime_support import Transport, config, isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs

LABEL = "local-model-2026"
# Translate, the EPUB, the default target language, keep the output location, then the Markdown offer.
GUIDED = "1\n{source}\n1\ny\n{markdown}\n"


def guided(tmp_path, monkeypatch, *, markdown="n"):
    source = _book(tmp_path / "book.epub")
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    result = runner.invoke(cli.app, [], input=GUIDED.format(source=source, markdown=markdown))
    return result, session


def test_guided_translation_with_a_label_runs_the_rich_runtime(tmp_path, monkeypatch):
    config(tmp_path, label=LABEL)
    result, session = guided(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    assert (tmp_path / "books" / "Traduzidos" / "book-pt.epub").is_file()
    job = _jobs(tmp_path)[-1]
    assert job.state is JobState.VALIDATED and job.request.command_version == 2
    provenance = records(tmp_path, job)
    assert provenance and all(record.producer.model == "declared:" + LABEL for record in provenance)
    assert any(method == "POST" for method, _, _ in session.requests)


def test_guided_translation_without_a_label_keeps_the_legacy_runtime(tmp_path, monkeypatch):
    config(tmp_path, label=None)
    result, _ = guided(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    assert (tmp_path / "books" / "Traduzidos" / "book-pt.epub").is_file()
    assert all(job.request.command_version == 1 for job in _jobs(tmp_path))


def test_guided_markdown_report_with_a_label_matches_the_terminal_report(tmp_path, monkeypatch):
    config(tmp_path, label=LABEL)
    result, _ = guided(tmp_path, monkeypatch, markdown="y")
    assert result.exit_code == 0, text(result)
    report = tmp_path / "books" / "Relatorios" / "book-pt-report.md"
    content = report.read_text(encoding="utf-8")
    assert content.startswith("# Translation report")
    output = tmp_path / "books" / "Traduzidos" / "book-pt.epub"
    assert f"- Output: {output}" in content
    assert "- Texts translated: 4" in content and "- Texts from cache: 1" in content
    assert "- Errors: 0" in content


def batch(tmp_path, monkeypatch, *arguments):
    first = _book(tmp_path / "a.epub")
    second = _book(tmp_path / "b.epub", chapter_two="<p>See you later.</p>")
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session",
                        lambda: Transport(failing="Goodbye reader."))
    return runner.invoke(cli.app, ["--mode", "developer", "translate", str(first), str(second), "--source", "en",
                                   "--target", "pt", "--retries", "0", "--cache", str(tmp_path / "cache.sqlite"),
                                   "--output-dir", str(tmp_path / "out"), *arguments])


@pytest.mark.parametrize("fail_fast", [False, True])
def test_a_failing_labeled_batch_stops_after_the_failed_book_by_default(tmp_path, monkeypatch, fail_fast):
    config(tmp_path, label=LABEL)
    result = batch(tmp_path, monkeypatch, *(["--fail-fast"] if fail_fast else []))
    assert result.exit_code == 1, text(result)
    assert "nenhum EPUB parcial" in " ".join(text(result).split())
    assert not list(tmp_path.glob("out/*.epub"))
    jobs = _jobs(tmp_path)
    assert [(job.state, job.request.command_version) for job in jobs] == [(JobState.FAILED, 2)]


def test_a_failing_labeled_batch_continues_on_request_without_a_partial_epub(tmp_path, monkeypatch):
    config(tmp_path, label=LABEL)
    result = batch(tmp_path, monkeypatch, "--continue-on-error")
    assert result.exit_code == 1, text(result)
    assert sorted(path.name for path in tmp_path.glob("out/*.epub")) == ["b-pt.epub"]
    states = sorted((job.state.value, job.request.command_version) for job in _jobs(tmp_path))
    assert states == [("failed", 2), ("validated", 2)]
