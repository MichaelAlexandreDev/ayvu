"""Real CLI, preflight and #128 composition; only the external HTTP wire is simulated."""
from __future__ import annotations

import io
import json
import shutil
import socket

import pytest
from typer.testing import CliRunner

from ayvu import cli, cli_composition
from ayvu.config_values import AyvuConfig
from ayvu.domain import LanguagePair, TranslationOptions
from ayvu.cli_contracts import PreflightPolicyError
from ayvu.network_policy import NetworkPolicy
from ayvu.provider_discovery import DiscoveryOutcome
from ayvu.provider_network import NetworkClass, ProviderOperation
from ayvu.resume import ResumeStateStore, TranslationResumeState
from test_network_policy import WireSocket, answer, deny_network, wire_reply

LANGUAGES = [{"code": "en", "name": "English", "targets": ["pt"]},
             {"code": "pt", "name": "Português", "targets": ["en"]}]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def provider_wire(monkeypatch):
    requests_seen, connections, resolutions = [], [], []

    class ProviderWire(WireSocket):
        def makefile(self, *args, **kwargs):
            header, body = b"".join(self.sent).split(b"\r\n\r\n", 1)
            method, path, _ = header.split(b"\r\n")[0].split()
            payload = json.loads(body) if body else None
            requests_seen.append((method.decode(), path.decode(), payload))
            if method == b"GET":
                assert path == b"/languages"
                response = LANGUAGES
            else:
                assert method == b"POST" and path == b"/translate"
                response = {"translatedText": "PT:" + payload["q"]}
            return io.BytesIO(wire_reply(json.dumps(response)))

    def resolve(*args):
        resolutions.append(args)
        return [answer()]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("uncontrolled DNS transport"))
    monkeypatch.setattr(socket, "socket", lambda family, *args: ProviderWire([], connections, family))
    return requests_seen, connections, resolutions


def preflight_values(tmp_path, book, policy):
    return dict(epub_path=book, cache_path=tmp_path / "cache.sqlite", glossary_path=None,
                translator_name="libretranslate", url="http://localhost:5000", timeout=2, retries=0,
                language_pair=LanguagePair("en", "pt"), dry_run=False, network_policy=policy)


def test_production_preflight_none_stops_at_policy_before_stores_or_network(minimal_epub_path, tmp_path, monkeypatch):
    reached = deny_network(monkeypatch)
    with pytest.raises(PreflightPolicyError) as caught:
        cli_composition.run_translation_preflight(**preflight_values(tmp_path, minimal_epub_path, "none"))
    assert "motivo=strict_offline" in caught.value.detail and "operacao=languages" in caught.value.detail
    assert caught.value.code == "strict_offline"
    assert reached == [] and not (tmp_path / "cache.sqlite").exists()


def test_shared_use_cases_and_cli_composition_use_the_same_snapshot_and_adapter(provider_wire):
    from ayvu.provider_composition import CLI_INSTANCE_ID
    session = cli_composition.cli_provider_session(url="http://localhost:5000", timeout=2, retries=0, network_policy="loopback")
    adapter = session._service._entries[CLI_INSTANCE_ID][1]
    assert adapter._translator.network_decision is session.network_decision
    for operation in (ProviderOperation.LANGUAGES, ProviderOperation.TEST, ProviderOperation.PREFLIGHT):
        assert session._decision(operation).network_class is NetworkClass.LOOPBACK_ONLY
    assert session.languages().outcome is DiscoveryOutcome.OK
    assert session.preflight("en", "pt").outcome is DiscoveryOutcome.OK
    assert session.test("en", "pt").outcome is DiscoveryOutcome.OK
    seen, _, resolutions = provider_wire
    assert [item[:2] for item in seen] == [("GET", "/languages"), ("GET", "/languages"), ("POST", "/translate")]
    assert seen[-1][2]["q"] == "Hello world" and len(resolutions) == 1


@pytest.mark.parametrize("operation", ["languages", "test", "preflight"])
def test_shared_use_cases_classify_unsafe_dns_as_policy_denial(monkeypatch, operation):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [answer(), answer("10.0.0.1")])
    monkeypatch.setattr(socket, "socket", lambda *args: pytest.fail("mixed DNS reached socket"))
    session = cli_composition.cli_provider_session(url="http://localhost:5000", timeout=1, retries=0)
    result = session.languages() if operation == "languages" else getattr(session, operation)("en", "pt")
    assert result.outcome is DiscoveryOutcome.POLICY_DENIED and result.code == "unsafe_resolution"


def test_preflight_and_worker_factory_share_one_resolution_and_policy(minimal_epub_path, tmp_path, provider_wire):
    result = cli_composition.run_translation_preflight(**preflight_values(tmp_path, minimal_epub_path, None))
    worker = result.translator_factory()
    assert worker.network_authorization is result.translator.network_authorization
    assert worker.network_decision is result.network_decision and result.network_decision.defaulted
    assert worker.translate("Worker text", "en", "pt") == "PT:Worker text"
    assert len(provider_wire[2]) == 1
    assert result.translator._session is not worker._session


@pytest.mark.parametrize("cache_only", [False, True])
def test_dry_run_and_cache_only_preserve_zero_network(minimal_epub_path, tmp_path, monkeypatch, cache_only):
    reached = deny_network(monkeypatch)
    values = preflight_values(tmp_path, minimal_epub_path, "none")
    values.update(dry_run=not cache_only, cache_only=cache_only)
    prepared = cli_composition.run_translation_preflight(**values)
    assert prepared.route is None and reached == []
    assert prepared.translator is None if cache_only else prepared.translator._session is None


@pytest.mark.parametrize("policy", ["none", "loopback"])
def test_guided_language_discovery_inherits_root_selection_and_does_not_persist_it(tmp_path, provider_wire, policy):
    result = CliRunner().invoke(cli.app, ["--network-policy", policy], input=f"pt\n{tmp_path / 'books'}\ny\n0\n")
    assert result.exit_code == 0, result.output
    if policy == "none":
        assert "strict_offline" in result.output and provider_wire[0] == [] and provider_wire[2] == []
    else:
        assert [item[:2] for item in provider_wire[0]] == [("GET", "/languages")]
    for config in tmp_path.rglob("config.json"):
        assert "network_policy" not in config.read_text()
    assert cli._selected_network_policy() is None  # Scoped input cannot leak into the next invocation.


@pytest.mark.parametrize("policy", ["none", "loopback"])
def test_preview_uses_real_preflight_and_policy(minimal_epub_path, tmp_path, provider_wire, policy):
    original = minimal_epub_path.read_bytes()
    result = CliRunner().invoke(cli.app, ["--network-policy", policy, "--preview", str(minimal_epub_path)])
    if policy == "none":
        assert result.exit_code == 1 and "strict_offline" in result.output
        assert provider_wire[0] == provider_wire[2] == []
    else:
        assert result.exit_code == 0, result.output
        assert provider_wire[0][0][:2] == ("GET", "/languages")
        assert any(item[0] == "POST" for item in provider_wire[0])
    assert minimal_epub_path.read_bytes() == original


@pytest.mark.parametrize("policy", ["none", "loopback"])
@pytest.mark.parametrize("batch", [False, True])
def test_translate_and_batch_use_real_production_composition(minimal_epub_path, tmp_path, provider_wire, policy, batch):
    books = [minimal_epub_path]
    if batch:
        other = tmp_path / "second.epub"
        shutil.copyfile(minimal_epub_path, other)
        books.append(other)
    originals = [book.read_bytes() for book in books]
    arguments = ["--mode", "developer", "translate", *map(str, books), "--network-policy", policy,
                 "--source", "en", "--target", "pt", "--retries", "0", "--cache", str(tmp_path / "cache.sqlite"),
                 "--output-dir", str(tmp_path / "translated")]
    if batch:
        arguments += ["--continue-on-error"]
    result = CliRunner().invoke(cli.app, arguments)
    if policy == "none":
        assert result.exit_code == 1 and "strict_offline" in result.output, result.output
        assert provider_wire[0] == provider_wire[2] == []
    else:
        assert result.exit_code == 0, result.output
        assert sum(item[0] == "GET" for item in provider_wire[0]) == len(books)
        assert any(item[0] == "POST" for item in provider_wire[0])
    assert [book.read_bytes() for book in books] == originals
    for checkpoint in tmp_path.rglob("*.ayvu-state.json"):
        assert "network_policy" not in checkpoint.read_text()


@pytest.mark.parametrize("policy", ["none", "loopback"])
def test_resume_uses_saved_endpoint_but_current_request_policy(minimal_epub_path, tmp_path, provider_wire, policy):
    # A legacy checkpoint is an existing supported production recovery input.
    processing = cli._processing_dir(AyvuConfig.default())
    state = TranslationResumeState.create(input_path=minimal_epub_path, output_path=tmp_path / "out.epub",
        cache_path=tmp_path / "cache.sqlite", translator_name="libretranslate", url="http://localhost:5000",
        glossary_path=None, options=TranslationOptions(LanguagePair("en", "pt")), overwrite=False, timeout=2, retries=0)
    ResumeStateStore(processing).save(state)
    result = CliRunner().invoke(cli.app, ["--mode", "developer", "resume", "--network-policy", policy])
    if policy == "none":
        assert result.exit_code == 1 and "strict_offline" in result.output, result.output
        assert provider_wire[0] == provider_wire[2] == []
    else:
        assert result.exit_code == 0, result.output
        assert provider_wire[0][0][:2] == ("GET", "/languages")
        assert any(item[0] == "POST" for item in provider_wire[0])


def test_external_reader_launch_is_outside_http_offline_receipt(tmp_path, monkeypatch, capsys):
    from ayvu.library_adapters import LocalLibraryFilesystem, ReaderLauncher
    from ayvu.library_service import LibraryService
    root = tmp_path / "books"
    (root / "Original").mkdir(parents=True)
    (root / "Original" / "Book.epub").write_bytes(b"characterization")
    invocations = []
    fs = LocalLibraryFilesystem(root, "Original", "Traduzidos")
    service = LibraryService(fs, ReaderLauncher("reader", runner=lambda *a, **k: invocations.append((a, k))), fs)
    ref = service.scan().books[0].original.ref
    reached = deny_network(monkeypatch)
    session = cli_composition.cli_provider_session(url="http://localhost:5000", timeout=1, retries=0, network_policy="none")
    assert session.languages().outcome is DiscoveryOutcome.POLICY_DENIED
    cli._open_library_book_path(service, ref, "Original")
    assert len(invocations) == 1 and reached == []
    assert "fora da garantia offline" in capsys.readouterr().out
    result = service.open(ref)
    assert result.status == "opened" and not hasattr(result, "network_decision")
