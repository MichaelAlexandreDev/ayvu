from tests.support.cli_support import isolated_config
from tests.support.cli_support import _fake_http, _http


def test_guided_selection_warns_with_a_code_and_not_the_response(monkeypatch, capsys):
    from ayvu.interfaces.cli.guided import language as cli_module

    _fake_http(monkeypatch, get=[_http(500, "SECRETBODY")] * 2)
    assert cli_module._load_languages_for_guided_selection() == ()
    output = capsys.readouterr().out
    assert "provider_server_error" in output and "SECRETBODY" not in output
