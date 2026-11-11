import pytest
from ayvu.interfaces.cli.app import app
from ayvu.providers.libretranslate.client import TranslatorError, TranslatorLanguage
from test_cli import _fake_http, _http, runner


def test_languages_command_lists_translator_languages(monkeypatch):
    calls: dict[str, object] = {}

    class FakeLanguageTranslator:
        def __init__(
            self,
            url: str,
            timeout: float,
            retries: int,
            requests_per_second: float | None = None,
            retry_backoff: float = 0.5,
            retry_backoff_max: float = 8.0,
            network_authorization=None,
        ) -> None:
            self.network_decision = network_authorization.decision
            calls["url"] = url
            calls["timeout"] = timeout
            calls["retries"] = retries
            calls["requests_per_second"] = requests_per_second
            calls["retry_backoff"] = retry_backoff
            calls["retry_backoff_max"] = retry_backoff_max

        def list_languages(self) -> tuple[TranslatorLanguage, ...]:
            return (
                TranslatorLanguage(code="pt", name="Portuguese", targets=("en", "es")),
                TranslatorLanguage(code="en", name="English", targets=("pt",)),
            )

    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FakeLanguageTranslator)

    result = runner.invoke(
        app,
        [
            "languages",
            "--url",
            "http://localhost:5000",
            "--timeout",
            "2",
            "--retries",
            "0",
            "--requests-per-second",
            "3.5",
            "--retry-backoff",
            "0.25",
            "--retry-backoff-max",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "url": "http://localhost:5000",
        "timeout": 2.0,
        "retries": 0,
        "requests_per_second": 3.5,
        "retry_backoff": 0.25,
        "retry_backoff_max": 2.0,
    }
    assert "LibreTranslate languages" in result.output
    assert "Portuguese" in result.output
    assert "pt" in result.output
    assert "installed" in result.output
    assert "en, es" in result.output


def test_languages_command_reports_failure_without_traceback(monkeypatch):
    class FailingLanguageTranslator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def list_languages(self) -> tuple[TranslatorLanguage, ...]:
            raise TranslatorError("server unavailable")

    monkeypatch.setattr("ayvu.composition.builders.providers.LibreTranslateTranslator", FailingLanguageTranslator)

    result = runner.invoke(app, ["languages"])

    assert result.exit_code == 1
    assert "Não foi possível listar os idiomas." in result.output
    assert "server unavailable" not in result.output
    assert "provider_request_failed" in result.output
    assert "Inicie o LibreTranslate" in result.output
    assert "Traceback" not in result.output


def test_test_translator_sends_only_the_known_text_and_reports_success(monkeypatch):
    session = _fake_http(monkeypatch, post=[_http(200, '{"translatedText": "Ola mundo"}')])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 0 and "Translator OK: Hello world -> Ola mundo" in result.output
    assert session.calls == [("POST", "http://localhost:5000/translate", {"q": "Hello world", "source": "en", "target": "pt", "format": "text"})]


def test_test_translator_help_discloses_the_text_it_sends():
    result = runner.invoke(app, ["test-translator", "--help"])
    assert result.exit_code == 0 and "Hello world" in result.output


def test_test_translator_treats_an_empty_translation_as_a_failure(monkeypatch):
    _fake_http(monkeypatch, post=[_http(200, '{"translatedText": ""}')])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "Translator OK" not in result.output and "empty_result" in result.output


@pytest.mark.parametrize("body", ["bad language pair SECRETBODY", "Call me Ishmael."])
def test_test_translator_failure_shows_a_code_and_not_the_response(monkeypatch, body):
    _fake_http(monkeypatch, post=[_http(400, body)])
    result = runner.invoke(app, ["test-translator", "--retries", "0"])
    assert result.exit_code == 1 and "O teste do tradutor falhou." in result.output
    assert "provider_rejected" in result.output and body not in result.output


def test_test_translator_refuses_an_invalid_language_before_sending(monkeypatch):
    session = _fake_http(monkeypatch)
    result = runner.invoke(app, ["test-translator", "--source", "x y"])
    assert result.exit_code == 1 and "invalid_locale" in result.output and session.calls == []


def test_languages_keeps_the_table_and_only_asks_for_languages(monkeypatch):
    session = _fake_http(monkeypatch, get=[_http(200, '[{"code": "pt", "name": "Portuguese", "targets": ["en", "es"]}]')])
    result = runner.invoke(app, ["languages", "--retries", "0"])
    assert result.exit_code == 0 and "Portuguese" in result.output and "en, es" in result.output and "installed" in result.output
    assert session.calls == [("GET", "http://localhost:5000/languages")]


def test_languages_with_an_empty_list_still_succeeds(monkeypatch):
    _fake_http(monkeypatch, get=[_http(200, "[]")])
    result = runner.invoke(app, ["languages", "--retries", "0"])
    assert result.exit_code == 0 and "LibreTranslate languages" in result.output
