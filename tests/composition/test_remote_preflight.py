"""The remote preflight's order (#379): every refusal comes before the book is read and before any request."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ayvu.composition.builders import remote, remote_consent, rich_provider, selection
from ayvu.kernel import LanguagePair
from ayvu.preferences import AyvuConfig
from ayvu.projects.schema import open_remote_consents
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.translation import PreflightError, PreflightPolicyError
from tests.support.remote_fakes import CREDENTIAL_ENV, HOST, MODEL, RETENTION, SECRET, fake_remote, wire  # noqa: F401

PAIR = LanguagePair("en", "pt")


class Asked:
    def __init__(self, answer):
        self.answer, self.notices = answer, []

    def __call__(self, notice):
        self.notices.append(notice)
        return self.answer


def project(tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes(b"synthetic")
    manifest = ProjectJsonRepository(tmp_path / "processing").create(build_project_request(source, source="en", target="pt"))
    return SimpleNamespace(processing_dir=tmp_path / "processing", project_id=manifest.project_id)


def preflight(*, book_checks, **values):
    configuration = dict(translator_name="fake_remote", epub_path="book.epub", cache_path="cache.sqlite",
                         glossary_path=None, url=None, timeout=5.0, language_pair=PAIR, network_policy="remote",
                         cache_check=None, model=MODEL, workers=1)
    return remote.remote_preflight(book_checks=book_checks, **{**configuration, **values})


def checks(calls):
    def book_checks(**values):
        calls.append(values)
        return {}
    return book_checks


def test_consent_is_asked_once_then_the_book_is_checked_and_nothing_is_sent(tmp_path, fake_remote, wire):
    book, calls = project(tmp_path), []
    asked = Asked(True)
    book.remote_consent = asked
    with remote_consent.consent_scope(remote_consent.project_gate(book)):
        prepared = preflight(book_checks=checks(calls))
        preflight(book_checks=checks(calls))
    assert [(item.provider, item.hosts, item.retention_claim) for item in asked.notices] == [
        ("fake_remote", (HOST, "files.fake-remote.test"), RETENTION)]
    assert len(calls) == 2 and wire.requests == []
    assert prepared.network_decision.remote and prepared.retention_claim == RETENTION
    assert prepared.binding.producer.provider == "fake_remote" and SECRET not in repr(prepared)


def test_a_refused_consent_stops_before_the_book_and_records_nothing(tmp_path, fake_remote, wire):
    book, calls = project(tmp_path), []
    book.remote_consent = Asked(False)
    with remote_consent.consent_scope(remote_consent.project_gate(book)), pytest.raises(PreflightError) as caught:
        preflight(book_checks=checks(calls))
    assert caught.value.detail == "remote_consent_refused" and calls == [] and wire.requests == []
    paths = ProjectJsonRepository(book.processing_dir).resolve_database_paths(book.project_id)
    assert open_remote_consents(paths.database, paths.backups, book.project_id).list() == ()


@pytest.mark.parametrize("values,error,code", [
    ({"network_policy": None}, PreflightPolicyError, "unsupported_scheme"),
    ({"network_policy": "loopback"}, PreflightPolicyError, "unsupported_scheme"),
    ({"network_policy": "none"}, PreflightPolicyError, "strict_offline"),
    ({"url": "https://evil.test/v1"}, PreflightPolicyError, "unsupported_destination"),
    ({"url": "http://localhost:5000"}, PreflightPolicyError, "unsupported_destination"),
    ({"model": None}, PreflightError, None),
    ({"workers": 2}, PreflightError, None),
    ({"dry_run": True}, PreflightError, None),
    ({"cache_only": True}, PreflightError, None),
    ({"language_pair": LanguagePair("en", "ja")}, PreflightError, None),
])
def test_what_a_remote_run_cannot_do_is_refused_before_consent(tmp_path, fake_remote, wire, values, error, code):
    book, calls, asked = project(tmp_path), [], Asked(True)
    book.remote_consent = asked
    with remote_consent.consent_scope(remote_consent.project_gate(book)), pytest.raises(error) as caught:
        preflight(book_checks=checks(calls), **values)
    assert getattr(caught.value, "code", None) == code if code else True
    assert asked.notices == [] and calls == [] and wire.requests == []


def test_a_missing_credential_is_refused_before_consent(tmp_path, fake_remote, wire, monkeypatch):
    monkeypatch.delenv(CREDENTIAL_ENV)
    book, asked = project(tmp_path), Asked(True)
    book.remote_consent = asked
    with remote_consent.consent_scope(remote_consent.project_gate(book)), pytest.raises(PreflightError) as caught:
        preflight(book_checks=checks([]))
    assert CREDENTIAL_ENV in caught.value.next_step and asked.notices == [] and wire.requests == []


def test_without_a_project_there_is_no_gate_and_the_run_is_refused(tmp_path, fake_remote, wire):
    calls = []
    with pytest.raises(PreflightError) as caught:
        preflight(book_checks=checks(calls))
    assert caught.value.detail == "remote_without_project" and calls == []
    assert rich_provider.prepare_model_translation("fake_remote", lambda: "prepared", SimpleNamespace()) == "prepared"
    assert remote_consent.current_gate() is None


def test_selection_and_identity_treat_a_registered_remote_provider_as_a_model_translator(fake_remote):
    assert rich_provider.identity_source("fake_remote") == rich_provider.IdentitySource.REMOTE_MODEL
    assert rich_provider.names_model("fake_remote") and not rich_provider.discovers_identity("fake_remote")
    chosen = selection.select_translator(AyvuConfig(translator_models={"fake_remote": MODEL}), translator="fake_remote")
    assert (chosen.translator, chosen.model, chosen.url) == ("fake_remote", MODEL, f"https://{HOST}")
    for flags in ({"cache_only": True}, {"preview": True}):
        with pytest.raises(PreflightError, match="remote provider"):
            selection.select_translator(AyvuConfig(), translator="fake_remote", **flags)
    # A dry run only estimates on this machine (#380), so selection lets it through.
    assert selection.select_translator(AyvuConfig(), translator="fake_remote", dry_run=True).translator == "fake_remote"


def test_without_a_registered_adapter_nothing_is_remote():
    assert remote.REMOTE_ADAPTERS == {}
    assert not remote.is_remote("anthropic") and remote.egress_notice("libretranslate") is None
    assert rich_provider.identity_source("ollama") == rich_provider.IdentitySource.DISCOVERED_MODEL
