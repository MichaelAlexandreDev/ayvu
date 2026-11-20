"""A fake remote model provider and an instrumented wire for the ``remote`` policy (#379).

No real provider and no network: ``FakeRemoteAdapter`` declares its hosts, retention claim and credential
variable like a real adapter would, and ``Wire`` replaces only ``HTTPAdapter.send``, the last step before
a socket, so the policy checks of ``network.remote_http`` all run. The wire answers the LLM core's
structured schema the way a model would, and records every request it was handed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest
import requests
from requests.adapters import HTTPAdapter

from ayvu.network.remote_http import RemoteTransportError
from ayvu.preferences.model import MODEL_TRANSLATORS
from ayvu.provenance import ProducerIdentity, declared_model, valid_observed_model_name
from ayvu.providers.capabilities import (
    Capability, CapabilityFact, EvidenceSource, LimitOrigin, LimitScope, LimitSemantics, LimitUnit, ProviderDescriptor,
    ProviderLimit, SupportState,
)
from ayvu.providers.llm import LLM_CORE_VERSION, FinishReason, ModelCallError, ModelResponse, TransportReason, llm_snapshot

SECRET = "sk-fixture-SECRET-7f3a9c"
HOST = "api.fake-remote.test"
MODEL = "fake-model-1"
CREDENTIAL_ENV = "FAKE_REMOTE_API_KEY"
RETENTION = "fixture terms: kept up to 30 days, never used for training"


def _fact(capability, state, evidence):
    return CapabilityFact(capability, state, evidence)


TRANSPORT_FACTS = (
    _fact(Capability.MODEL_IDENTITY, SupportState.SUPPORTED, EvidenceSource.CONFIGURATION),
    _fact(Capability.LANGUAGE_DISCOVERY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.ROUTE_DISCOVERY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.IN_FLIGHT_CANCELLATION, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.SHARED_INSTANCE_CONCURRENCY, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.AUTHENTICATION, SupportState.SUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.COST_REPORTING, SupportState.UNSUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.NETWORK_CLASS, SupportState.SUPPORTED, EvidenceSource.ADAPTER_CODE),
    _fact(Capability.RETENTION_CLAIM, SupportState.SUPPORTED, EvidenceSource.PROVIDER_DOCUMENTATION),
    *(_fact(item, SupportState.UNKNOWN, EvidenceSource.NONE) for item in (
        Capability.TRAINING_CLAIM, Capability.RESOURCE_REPORTING)),
)


class FakeRemoteModel:
    def __init__(self, client, credential, name):
        self._client, self._credential, self._name = client, credential, name

    def complete(self, request):
        body = {"model": self._name, "system": request.system, "user": request.user, "schema": request.response_schema}
        try:
            response = self._client.post_json("/v1/translate", body, headers={"Accept": "application/json"},
                                              credential_headers={"Authorization": f"Bearer {self._credential.reveal()}"})
        except RemoteTransportError as error:
            raise ModelCallError(TransportReason.POLICY if error.reason == "policy" else TransportReason.CONNECTION)
        if response.status_code >= 400:
            raise ModelCallError(TransportReason.HTTP, status_code=response.status_code)
        return ModelResponse(FinishReason.STOP, json.loads(response.content)["output"])

    def close(self):
        self._client.close()


class FakeRemoteAdapter:
    ADAPTER_ID = "fake_remote"
    BASE_URL = f"https://{HOST}"
    ALLOWED_HOSTS = (HOST, "files.fake-remote.test")
    RETENTION_CLAIM = RETENTION
    CREDENTIAL_ENV = CREDENTIAL_ENV
    DECLARED_CODES = frozenset({"en", "pt", "es"})
    DESCRIPTOR = ProviderDescriptor("fake_remote", 1, "fake_remote_https", 1, (1,), (1,))

    def producer(self, model):
        return ProducerIdentity("fake_remote", declared_model(model), "fake_remote_https@1", f"llm_core@{LLM_CORE_VERSION}")

    def snapshot(self, model, observed_at: datetime, *, timeout_seconds):
        limits = (ProviderLimit("client_request_timeout", LimitUnit.MILLISECONDS, LimitScope.REQUEST,
                                LimitOrigin.CLIENT_POLICY, LimitSemantics.KNOWN, EvidenceSource.CONFIGURATION,
                                timeout_seconds * 1000),)
        return llm_snapshot(self.DESCRIPTOR, TRANSPORT_FACTS, limits, observed_at,
                            configuration_ref=f"fake_remote_model:{hashlib.sha256(model.encode()).hexdigest()}")

    def model(self, client, credential, model):
        return FakeRemoteModel(client, credential, model)


def translated(text):
    return f"PT[{text}]"


class Wire:
    """Stands in for ``HTTPAdapter.send``. ``script`` holds per-request steps returning (status, payload, headers)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list = []

    def __call__(self, adapter, request, **kwargs):
        body = json.loads(request.body) if request.body else None
        self.requests.append({"url": request.url, "method": request.method, "headers": dict(request.headers),
                              "body": body, "proxies": kwargs.get("proxies"), "verify": kwargs.get("verify")})
        status, payload, headers = self.script.pop(0)(body) if self.script else (200, self.answer(body), {})
        response = requests.Response()
        response.status_code, response.url, response.request = status, request.url, request
        response.headers.update(headers)
        response._content = json.dumps(payload).encode()
        return response

    @staticmethod
    def answer(body):
        if not isinstance(body, dict) or "user" not in body:
            return {"ok": True}
        segments = json.loads(body["user"])["segments"]
        return {"output": json.dumps({"translations": [{"id": item["id"], "text": translated(item["text"])}
                                                       for item in segments]})}


@pytest.fixture
def wire(monkeypatch):
    fake = Wire()
    monkeypatch.setattr(HTTPAdapter, "send", lambda adapter, request, **kwargs: fake(adapter, request, **kwargs))
    return fake


@pytest.fixture
def fake_remote(monkeypatch):
    """Registers the fake adapter the way a provider issue registers a real one, with its credential set."""
    from ayvu.composition.builders import remote

    monkeypatch.setitem(remote.REMOTE_ADAPTERS, FakeRemoteAdapter.ADAPTER_ID, FakeRemoteAdapter())
    monkeypatch.setitem(MODEL_TRANSLATORS, FakeRemoteAdapter.ADAPTER_ID, valid_observed_model_name)
    monkeypatch.setenv(CREDENTIAL_ENV, SECRET)
    return FakeRemoteAdapter
