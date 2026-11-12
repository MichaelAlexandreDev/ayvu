import pytest
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import _fake_http, _http
from tests.support.cli_support import isolated_config, runner


def test_cli_console_width_does_not_depend_on_the_environment():
    from ayvu.interfaces.cli.render import console

    assert console.width == 500


def test_common_mode_failure_shows_no_technical_detail(monkeypatch):
    _fake_http(monkeypatch, post=[_http(500, "oops")])
    result = runner.invoke(app, ["--mode", "common", "test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "Detalhe técnico" not in result.output and "provider_server_error" not in result.output


@pytest.mark.parametrize("command", ["languages", "test-translator"])
def test_bad_settings_report_a_code_without_a_traceback(command):
    result = runner.invoke(app, [command, "--requests-per-second", "0"])
    assert result.exit_code == 1 and "invalid_provider_settings" in result.output and "Traceback" not in result.output


def test_credentials_in_the_url_never_reach_the_output(monkeypatch):
    import requests as requests_module

    url = "http://user:hunter2@private.example.test:5000"
    _fake_http(monkeypatch, get=[requests_module.exceptions.ConnectionError(f"cannot reach {url}")], post=[requests_module.exceptions.ConnectionError(f"cannot reach {url}")])
    for command in ("languages", "test-translator"):
        result = runner.invoke(app, [command, "--url", url, "--retries", "0"])
        assert result.exit_code == 1 and "hunter2" not in result.output and "private.example.test" not in result.output
