from ayvu.interfaces.cli.app import app
from ayvu.recovery.checkpoint import ResumeStateStore
from tests.support.cli_support import _mock_translation_pipeline
from tests.support.cli_support import isolated_config, runner


def test_translate_command_passes_execution_controls_to_preflight_and_resume(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    processing_dir = tmp_path / "Processando"
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--requests-per-second",
            "3.5",
            "--retry-backoff",
            "0.25",
            "--retry-backoff-max",
            "2",
            "--workers",
            "3",
        ],
    )

    preflight = calls["preflight"]
    state_paths = list(processing_dir.glob("*.ayvu-state.json"))
    saved_state = ResumeStateStore(processing_dir).load(state_paths[0])
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 3.5
    assert preflight["retry_backoff"] == 0.25
    assert preflight["retry_backoff_max"] == 2.0
    assert calls["options"].workers == 3
    assert saved_state.requests_per_second == 3.5
    assert saved_state.retry_backoff == 0.25
    assert saved_state.retry_backoff_max == 2.0
    assert saved_state.workers == 3
    assert "Workers" in result.output
    assert "3 (parallel" in result.output
    assert "3.5 requests/second" in result.output
    assert "shared across workers" in result.output
    assert "2 retries" in result.output
    assert "0.25s initial backoff" in result.output


def test_translate_command_uses_low_performance_profile(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "low",
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 1.0
    assert preflight["retries"] == 4
    assert preflight["retry_backoff"] == 1.0
    assert preflight["retry_backoff_max"] == 12.0
    assert options.workers == 1
    assert options.chunk_limit == 1500
    assert "Performance profile" in result.output
    assert "low" in result.output
    assert "1 requests/second" in result.output
    assert "4 retries" in result.output


def test_translate_command_explicit_execution_flags_override_performance_profile(
    minimal_epub_path,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "high",
            "--workers",
            "2",
            "--requests-per-second",
            "3",
            "--retries",
            "5",
            "--retry-backoff",
            "0.75",
            "--retry-backoff-max",
            "6",
            "--chunk-limit",
            "2200",
        ],
    )

    preflight = calls["preflight"]
    options = calls["options"]
    assert result.exit_code == 0
    assert preflight["requests_per_second"] == 3.0
    assert preflight["retries"] == 5
    assert preflight["retry_backoff"] == 0.75
    assert preflight["retry_backoff_max"] == 6.0
    assert options.workers == 2
    assert options.chunk_limit == 2200
    assert "Performance profile" in result.output
    assert "high" in result.output
    assert "2 (parallel" in result.output
    assert "3 requests/second shared across workers" in result.output
