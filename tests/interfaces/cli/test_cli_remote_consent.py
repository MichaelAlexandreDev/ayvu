"""Remote translation from the CLI, end to end, with a fake remote adapter and an instrumented wire (#379).

The production composition, consent store, LLM core and ``network.remote_http`` all run; only the
provider's server is fake. HOME, XDG_CONFIG_HOME and the working directory are isolated by ``isolate``.
"""
from __future__ import annotations

import json

from ayvu.interfaces.cli import app as cli
from ayvu.jobs.model import JobState
from ayvu.projects.schema import open_remote_consents
from ayvu.projects.store import ProjectJsonRepository
from tests.support.cli_rich_runtime_support import isolate, records, runner, text  # noqa: F401
from tests.support.cli_translate_project_support import _book, _jobs
from tests.support.remote_fakes import HOST, MODEL, RETENTION, SECRET, fake_remote, wire  # noqa: F401


def configure(tmp_path):
    path = tmp_path / "config" / "ayvu" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"books_dir": str(tmp_path / "books"), "translator_models": {"fake_remote": MODEL}}))


def translate(tmp_path, *root, more=(), mode="developer", stdin=None):
    source = tmp_path / "book.epub"
    if not source.exists():
        _book(source)
    arguments = ["--mode", mode, *root, "translate", str(source), "--source", "en", "--target", "pt",
                 "--translator", "fake_remote", "--network-policy", "remote", "--output", str(tmp_path / "out.epub"),
                 "--overwrite", *more]
    if mode == "developer":
        arguments += ["--cache", str(tmp_path / "cache.sqlite"), "--retries", "0"]
    return runner.invoke(cli.app, arguments, input=stdin)


def consents(tmp_path):
    repository = ProjectJsonRepository(tmp_path / "processing")
    found = []
    for project in repository.list_projects():
        paths = repository.resolve_database_paths(project.project_id)
        if paths.database.exists():
            found.extend(open_remote_consents(paths.database, paths.backups, project.project_id).list())
    return found


def test_developer_mode_without_the_flag_is_refused_with_zero_requests(tmp_path, fake_remote, wire):
    configure(tmp_path)
    result = translate(tmp_path)
    assert result.exit_code == 1, text(result)
    assert "Remote translation needs your consent. Nothing was sent." in text(result)
    assert "--consent-remote" in text(result)
    assert wire.requests == [] and consents(tmp_path) == []
    assert [job for job in _jobs(tmp_path) if job.request.command_type == "translate_project"] == []
    assert not (tmp_path / "out.epub").exists()


def test_the_developer_flag_records_consent_and_translates_over_https(tmp_path, fake_remote, wire):
    configure(tmp_path)
    result = translate(tmp_path, "--consent-remote")
    assert result.exit_code == 0, text(result)
    [consent] = consents(tmp_path)
    assert (consent.provider, consent.text_version) == ("fake_remote", "remote-egress-v1")
    assert consent.granted_at.utcoffset().total_seconds() == 0
    assert wire.requests and {item["url"] for item in wire.requests} == {f"https://{HOST}/v1/translate"}
    assert all(item["headers"]["Authorization"] == f"Bearer {SECRET}" for item in wire.requests)
    assert all(item["proxies"] == {} and item["verify"] is True for item in wire.requests)
    # The plan and the report both name the provider's retention claim and where the text goes.
    output = " ".join(text(result).split())
    assert output.count(RETENTION) >= 2 and f"{HOST}, files.fake-remote.test (HTTPS)" in output
    job = next(job for job in _jobs(tmp_path) if job.request.command_type == "translate_project")
    assert job.state is JobState.VALIDATED
    assert {(item.provider, item.model) for item in (r.producer for r in records(tmp_path, job))} == {
        ("fake_remote", f"declared:{MODEL}")}


def test_common_mode_asks_once_in_plain_words_and_no_sends_nothing(tmp_path, fake_remote, wire):
    configure(tmp_path)
    refused = translate(tmp_path, mode="common", stdin="n\n")
    assert refused.exit_code == 1, text(refused)
    question = " ".join(text(refused).split())
    assert "The book's visible text and your glossary terms will leave this computer" in question
    assert "File names, folders and settings are not sent." in question and RETENTION in question
    assert wire.requests == [] and consents(tmp_path) == []
    granted = translate(tmp_path, mode="common", stdin="y\nn\n")  # consent, then no Markdown report
    assert granted.exit_code == 0, text(granted)
    assert "Send this book's text to fake_remote?" in text(granted)
    assert [item.provider for item in consents(tmp_path)] == ["fake_remote"]
    sent = len(wire.requests)
    assert sent > 0
    again = translate(tmp_path, mode="common", stdin="n\n")
    assert again.exit_code == 0, text(again)
    assert "Send this book's text" not in text(again)  # asked once: the recorded consent is enough


def test_revoked_consent_is_asked_again(tmp_path, fake_remote, wire):
    configure(tmp_path)
    assert translate(tmp_path, "--consent-remote").exit_code == 0
    revoked = runner.invoke(cli.app, ["consent", "revoke", str(tmp_path / "book.epub")])
    assert revoked.exit_code == 0, text(revoked)
    assert "Consent revoked: 1 record(s) in 1 project(s)." in " ".join(text(revoked).split())
    assert consents(tmp_path) == []
    before = len(wire.requests)
    refused = translate(tmp_path)
    assert refused.exit_code == 1 and "needs your consent" in text(refused)
    assert len(wire.requests) == before
    asked = translate(tmp_path, mode="common", stdin="y\nn\n")
    assert asked.exit_code == 0 and "Send this book's text to fake_remote?" in text(asked), text(asked)


def test_revoke_by_project_id_and_unknown_targets(tmp_path, fake_remote, wire):
    configure(tmp_path)
    assert translate(tmp_path, "--consent-remote").exit_code == 0
    [project] = ProjectJsonRepository(tmp_path / "processing").list_projects()
    unknown = runner.invoke(cli.app, ["consent", "revoke", "00000000-0000-0000-0000-000000000000"])
    assert unknown.exit_code == 1 and "No project was found" in text(unknown)
    other = runner.invoke(cli.app, ["consent", "revoke", project.project_id, "--translator", "another_remote"])
    assert other.exit_code == 0 and "Nothing changed." in text(other) and len(consents(tmp_path)) == 1
    by_id = runner.invoke(cli.app, ["consent", "revoke", project.project_id])
    assert by_id.exit_code == 0 and consents(tmp_path) == []


def test_the_secret_never_reaches_a_written_file_or_any_output(tmp_path, fake_remote, wire):
    configure(tmp_path)
    outputs = [translate(tmp_path, mode="common", stdin="y\nn\n"),  # the question, the plan and the report
               translate(tmp_path, "--consent-remote", more=("--review-output", str(tmp_path / "review.json"))),
               translate(tmp_path, "--consent-remote", "--network-policy", "remote", more=("--dry-run",))]
    assert [result.exit_code for result in outputs] == [0, 0, 1], [text(result) for result in outputs]
    assert wire.requests  # the secret was used, on the wire only
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert any(path.suffix == ".sqlite" for path in written) and (tmp_path / "review.json").exists()
    for path in written:
        assert SECRET.encode() not in path.read_bytes(), path
    assert all(SECRET not in result.output for result in outputs)


def test_remote_dry_run_and_cache_only_are_refused_before_any_request(tmp_path, fake_remote, wire):
    configure(tmp_path)
    for flags in (("--dry-run",), ("--cache-only",)):
        result = translate(tmp_path, "--consent-remote", more=flags)
        assert result.exit_code == 1 and "not available with a remote provider" in text(result), text(result)
    assert wire.requests == [] and consents(tmp_path) == []


def test_without_the_remote_policy_nothing_is_asked_or_sent(tmp_path, fake_remote, wire):
    configure(tmp_path)
    source = _book(tmp_path / "book.epub")
    result = runner.invoke(cli.app, ["--mode", "developer", "--consent-remote", "translate", str(source),
                                     "--source", "en", "--target", "pt", "--translator", "fake_remote",
                                     "--output", str(tmp_path / "out.epub")])
    assert result.exit_code == 1 and "unsupported_scheme" in text(result), text(result)
    assert "--network-policy remote" in text(result)
    assert wire.requests == [] and consents(tmp_path) == []
