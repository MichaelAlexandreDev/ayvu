"""G10: with a label, a provider failure stops the book, whatever --fail-fast and the worker count say."""
import pytest

from ayvu.interfaces.cli import app as cli
from ayvu.jobs import JobState
from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.rich_preview_support import preview_book

# Spine order is 08, 06, 09, 05, 10, 04, 11, 03, 12, 02, 13, 01, 07.
POSITIONS = {"first": "Document 08.", "middle": "Document 11.", "last": "Document 07."}


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("fail_fast", [False, True])
@pytest.mark.parametrize("workers", ["1", "4"])
def test_provider_failure_stops_the_book_and_resumes(tmp_path, monkeypatch, fail_fast, workers, position):
    failing = POSITIONS[position]
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    arguments = ["--workers", workers, *(["--fail-fast"] if fail_fast else [])]
    result, transport = invoke(tmp_path, monkeypatch, source, *arguments, session=Transport(failing=failing))
    assert result.exit_code == 1, text(result)
    assert "nenhum EPUB parcial" in " ".join(text(result).split())
    assert not (tmp_path / "out.epub").exists()
    assert not list(tmp_path.rglob("*-missing*.txt"))
    parent = _jobs(tmp_path)[-1]
    assert parent.state is JobState.FAILED and parent.terminal_summary.reason_code == "provider_server_error"
    resumed = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: resumed)
    result = runner.invoke(cli.app, ["--mode", "developer", "resume", str(source)])
    assert result.exit_code == 0, text(result)
    assert failing in resumed.posts
    assert any(job.continuation_of == parent.job_id and job.state is JobState.VALIDATED for job in _jobs(tmp_path))


def test_require_full_cache_refusal_is_in_portuguese_on_the_rich_runtime(tmp_path):
    from ayvu.composition.builders.rich_options import check_rich_options
    from ayvu.kernel import LanguagePair, TranslationOptions
    from ayvu.translation import PreflightError
    with pytest.raises(PreflightError) as refused:
        check_rich_options(TranslationOptions(language_pair=LanguagePair("en", "pt"), require_full_cache=True))
    assert refused.value.summary == "--require-full-cache exige --cache-only."
