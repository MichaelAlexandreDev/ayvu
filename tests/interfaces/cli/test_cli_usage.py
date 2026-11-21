"""Usage, cost, the --max-cost cap and the dry-run estimate from the CLI, end to end (#380).

Production composition, stores and the LLM core run; LibreTranslate and Ollama answer through in-process
sessions and the remote provider through the instrumented wire, so no real translator or network is used.
HOME, XDG_CONFIG_HOME and the working directory are isolated by ``isolate``.
"""
from __future__ import annotations

import json

from ayvu.interfaces.cli import app as cli
from ayvu.jobs.model import JobState
from ayvu.projects.schema import open_provider_usage, open_remote_consents
from ayvu.projects.store import ProjectJsonRepository
from tests.support.cli_rich_runtime_support import config, invoke, isolate, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.ollama_fakes import MODEL
from tests.support.ollama_selection_support import OllamaTransport
from tests.support.remote_fakes import MODEL as REMOTE_MODEL, fake_remote, wire  # noqa: F401


def flat(result) -> str:
    return " ".join(text(result).replace("│", " ").split())


def usage_rows(tmp_path):
    rows = []
    projects = ProjectJsonRepository(tmp_path / "processing")
    for job in _jobs(tmp_path):
        paths = projects.resolve_database_paths(job.request.project_id)
        rows += open_provider_usage(paths.database, paths.backups, job.request.project_id).records(job.job_id)
    return rows


def ollama(tmp_path, monkeypatch, *root, more=(), session=None, prices=None):
    config(tmp_path, label=None, translator_models={"ollama": MODEL}, **({"prices": prices} if prices else {}))
    source = tmp_path / "book.epub"
    if not source.exists():
        _book(source)
    session = session or OllamaTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    result = runner.invoke(cli.app, ["--mode", "developer", *root, "translate", str(source), "--translator", "ollama",
                                     "--source", "en", "--target", "pt", "--retries", "0",
                                     "--output", str(tmp_path / "out.epub"), *more])
    return result, session


def test_libretranslate_reports_characters_zero_tokens_and_zero_cost(tmp_path, monkeypatch):
    config(tmp_path)
    result, session = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"))
    assert result.exit_code == 0, text(result)
    output = flat(result)
    assert f"Provider calls {len(session.posts)}" in output
    assert "Tokens in / out / cached 0 / 0 / 0" in output and "Cost (USD) $0.00" in output
    assert "Price table 2026-11-20" in output
    rows = usage_rows(tmp_path)
    assert len(rows) == len(session.posts) and {row.provider for row in rows} == {"libretranslate"}
    assert all((row.input_tokens, row.output_tokens, row.cost_micro_usd) == (0, 0, 0) for row in rows)
    assert sum(row.characters for row in rows) == sum(map(len, session.posts))


def test_ollama_reports_its_tokens_at_zero_cost(tmp_path, monkeypatch):
    result, session = ollama(tmp_path, monkeypatch)
    assert result.exit_code == 0, text(result)
    chats = [body for method, _, body in session.requests if method == "POST"]
    output = flat(result)
    # The fake Ollama reports 12 prompt and 7 output tokens for every chat.
    assert f"Provider calls {len(chats)}" in output and "Cost (USD) $0.00" in output
    assert f"Tokens in / out / cached {12 * len(chats)} / {7 * len(chats)} / unknown" in output
    assert {(row.input_tokens, row.output_tokens, row.cost_micro_usd) for row in usage_rows(tmp_path)} == {(12, 7, 0)}


def test_the_cap_stops_the_job_and_a_resume_with_a_new_cap_finishes_it_counted_once(tmp_path, monkeypatch):
    # $1,000 per million tokens: each call reserves about $4.46 and really costs $0.019 (19 tokens).
    prices = {"ollama": {MODEL: {"input": "1000", "output": "1000"}}}
    result, session = ollama(tmp_path, monkeypatch, "--max-cost", "4.50", prices=prices)
    assert result.exit_code == 1, text(result)
    output = flat(result)
    assert "The cost cap was reached before the next request" in output
    assert "Spent in this run: $0.057 of the $4.50 cap (--max-cost). Provider calls: 3." in output
    assert not (tmp_path / "out.epub").exists()
    [parent] = [job for job in _jobs(tmp_path) if job.request.command_type == "translate_project"]
    assert parent.state is JobState.FAILED
    first_chats = sum(1 for method, _, _ in session.requests if method == "POST")
    assert first_chats == 3 == len(usage_rows(tmp_path))
    session.requests.clear()
    resumed = runner.invoke(cli.app, ["--mode", "developer", "--max-cost", "100", "resume", str(tmp_path / "book.epub")])
    assert resumed.exit_code == 0, text(resumed)
    assert (tmp_path / "out.epub").exists()
    second_chats = sum(1 for method, _, _ in session.requests if method == "POST")
    rows = usage_rows(tmp_path)
    assert len(rows) == first_chats + second_chats and len({row.request_id for row in rows}) == len(rows)
    output = flat(resumed)
    assert f"Provider calls {second_chats}" in output and "Price table config.json" in output
    assert f"Job total {first_chats + second_chats} calls, ${19 * (first_chats + second_chats) / 1000:.3f}" in output


def test_a_cap_with_no_known_price_is_refused_before_any_request(tmp_path, fake_remote, wire):
    config(tmp_path, label=None, translator_models={"fake_remote": REMOTE_MODEL})
    source = _book(tmp_path / "book.epub")
    result = runner.invoke(cli.app, ["--mode", "developer", "--consent-remote", "--max-cost", "1", "translate",
                                     str(source), "--translator", "fake_remote", "--network-policy", "remote",
                                     "--source", "en", "--target", "pt", "--output", str(tmp_path / "out.epub")])
    assert result.exit_code == 1 and "No price is known for fake_remote" in flat(result), text(result)
    assert wire.requests == []
    for project in ProjectJsonRepository(tmp_path / "processing").list_projects():
        paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(project.project_id)
        assert not paths.database.exists() or open_remote_consents(paths.database, paths.backups,
                                                                   project.project_id).list() == ()


def test_an_ollama_dry_run_estimates_with_zero_requests_and_matches_the_real_calls(tmp_path, monkeypatch):
    dry, session = ollama(tmp_path, monkeypatch, more=("--dry-run",))
    assert dry.exit_code == 0, text(dry)
    assert session.requests == []
    output = flat(dry)
    assert "Tokens in / out (approximate)" in output and "Cost (approximate, USD) $0.00" in output
    assert "Worst case (what --max-cost reserves) $0.00" in output and "Nothing was sent" in output
    estimated = int(output.split("Estimated calls ", 1)[1].split()[0])
    real, session = ollama(tmp_path, monkeypatch, session=session)
    assert real.exit_code == 0, text(real)
    assert estimated == sum(1 for method, _, _ in session.requests if method == "POST")


def test_a_remote_dry_run_estimates_a_configured_price_without_consent_or_requests(tmp_path, fake_remote, wire):
    config(tmp_path, label=None, translator_models={"fake_remote": REMOTE_MODEL},
           prices={"fake_remote": {"*": {"input": "3", "output": "15"}}})
    source = _book(tmp_path / "book.epub")
    result = runner.invoke(cli.app, ["--mode", "developer", "translate", str(source), "--translator", "fake_remote",
                                     "--source", "en", "--target", "pt", "--dry-run"])
    assert result.exit_code == 0, text(result)
    output = flat(result)
    assert wire.requests == [] and "Price table config.json" in output
    cost = output.split("Cost (approximate, USD) ", 1)[1].split()[0]
    worst = output.split("Worst case (what --max-cost reserves) ", 1)[1].split()[0]
    assert cost.startswith("$0.0") and float(worst[1:]) > float(cost[1:]) > 0
    assert not (tmp_path / "processing" / "projects").exists() or ProjectJsonRepository(
        tmp_path / "processing").list_projects() == ()


def test_an_invalid_cap_or_price_is_refused_before_any_work(tmp_path, monkeypatch):
    result, session = ollama(tmp_path, monkeypatch, "--max-cost", "-1")
    assert result.exit_code == 2 and "--max-cost" in text(result) and session.requests == []
    path = tmp_path / "config" / "ayvu" / "config.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "prices": {"ollama": {MODEL: {"input": "free"}}}}))
    result = runner.invoke(cli.app, ["--mode", "developer", "translate", str(tmp_path / "book.epub"),
                                     "--translator", "ollama", "--source", "en", "--target", "pt"])
    assert result.exit_code == 1 and "Invalid price in prices." in text(result) and session.requests == []
