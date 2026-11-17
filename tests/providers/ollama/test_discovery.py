"""Ollama discovery and identity: the installed model, a missing one, and a digest change (#376)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ayvu.composition.builders.llm import LLM_REQUIREMENTS, llm_plan_inputs
from ayvu.network import resolve_network_policy
from ayvu.network.http import NetworkAuthorization
from ayvu.providers import Capability, ProviderRoute, ProviderSetupError, SupportState, capability_fingerprint
from ayvu.providers.capabilities import AvailabilityState
from ayvu.providers.llm import LlmSettings
from ayvu.providers.ollama.client import InstalledModel, OllamaClient
from ayvu.providers.ollama.discovery import (
    DECLARED_CODES, OllamaDiscoveryAdapter, find_model, ollama_producer, ollama_snapshot,
)
from ayvu.translation.plan import resolve_plan
from tests.support.ollama_fakes import DIGEST_A, DIGEST_B, MODEL, FakeOllama, fake_ollama  # noqa: F401

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
GLOSSARY = "glossary:" + "c" * 64


def client(url, policy="loopback"):
    return OllamaClient(NetworkAuthorization(resolve_network_policy(policy, url)), timeout=5.0)


def plan_for(model: InstalledModel):
    return resolve_plan(ollama_snapshot(model, NOW, timeout_seconds=30.0), LLM_REQUIREMENTS,
                        ollama_producer(model), ProviderRoute("en", "pt"), chunk_limit=3000,
                        **llm_plan_inputs(LlmSettings(), GLOSSARY))


def test_discovery_finds_the_selected_model_and_its_digest(fake_ollama):
    assert find_model(client(fake_ollama.url), MODEL) == InstalledModel(MODEL, DIGEST_A)


def test_a_missing_model_is_refused_with_a_code_before_any_chat(fake_ollama):
    with pytest.raises(ProviderSetupError) as raised:
        find_model(client(fake_ollama.url), "llama3.2:3b")
    assert str(raised.value) == "model_not_installed"
    assert fake_ollama.requests == [("GET", "/api/tags")]


def test_an_unreachable_server_is_a_code_too():
    server = FakeOllama().start()
    url = server.url
    server.close()
    with pytest.raises(ProviderSetupError) as raised:
        find_model(client(url), MODEL)
    assert str(raised.value) == "provider_unavailable"


def test_the_producer_names_the_model_and_its_digest():
    producer = ollama_producer(InstalledModel(MODEL, DIGEST_A))
    assert (producer.provider, producer.model) == ("ollama", f"{MODEL}@{DIGEST_A}")
    assert producer.fully_identified


def test_a_digest_change_gives_a_new_identity_and_a_new_fingerprint():
    before, after = InstalledModel(MODEL, DIGEST_A), InstalledModel(MODEL, DIGEST_B)
    assert ollama_producer(before) != ollama_producer(after)
    snapshot = lambda model: ollama_snapshot(model, NOW, timeout_seconds=30.0)  # noqa: E731
    assert capability_fingerprint(snapshot(before)) != capability_fingerprint(snapshot(after))
    assert plan_for(before).semantic_fingerprint != plan_for(after).semantic_fingerprint
    assert plan_for(before).semantic_fingerprint == plan_for(InstalledModel(MODEL, DIGEST_A)).semantic_fingerprint


def test_the_model_identity_is_observed_and_every_transport_fact_is_stated():
    facts = {fact.capability: fact for fact in ollama_snapshot(None, NOW, timeout_seconds=30.0).facts}
    assert set(facts) == set(Capability)
    assert facts[Capability.MODEL_IDENTITY].state is SupportState.SUPPORTED
    assert facts[Capability.MODEL_IDENTITY].evidence.value == "runtime_observation"
    assert facts[Capability.STRUCTURED_OUTPUT].state is SupportState.SUPPORTED


def test_the_languages_are_declared_and_need_the_server_to_answer(fake_ollama):
    observed = OllamaDiscoveryAdapter(client(fake_ollama.url), None, clock=lambda: NOW).observe()
    assert observed.availability.state is AvailabilityState.AVAILABLE
    assert {item.code for item in observed.languages} == DECLARED_CODES
    assert all(item.state == "declared" for item in observed.languages)


def test_a_selected_model_that_is_missing_degrades_availability(fake_ollama):
    observed = OllamaDiscoveryAdapter(client(fake_ollama.url), "missing:1b", clock=lambda: NOW).observe()
    assert (observed.availability.state, observed.availability.reason_code) == (AvailabilityState.DEGRADED,
                                                                             "model_not_installed")


def test_a_declared_pair_is_a_direct_route_and_nothing_else(fake_ollama):
    adapter = OllamaDiscoveryAdapter(client(fake_ollama.url), None, clock=lambda: NOW)
    availability = adapter.observe().availability
    assert adapter.route(availability, "en", "pt", NOW, ("en",)) == ProviderRoute("en", "pt")
    assert adapter.route(availability, "en", "xx", NOW, ("en",)) is None
