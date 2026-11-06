"""A fake provider and factory that register without any name conditional, and a fake HTTP session."""
from __future__ import annotations

from uuid import UUID

import requests

from ayvu.job_events import JobFailure, JobFailureFamily
from ayvu.provenance import ProducerIdentity
from ayvu.provider import DeliveryState, ProviderStatus, TranslationProviderRequest, TranslationProviderResult
from ayvu.provider_service import ProviderInstance

REQUEST_ID = str(UUID(int=7))
FAKE_PRODUCER = ProducerIdentity('fake_engine@1', 'fake_model@2', 'fake_adapter@1', 'not_applicable')


def request(text='Hello', source='en', target='pt', **changes) -> TranslationProviderRequest:
    return TranslationProviderRequest(**{'request_id': REQUEST_ID, 'text': text, 'source_locale': source,
                                         'target_locale': target, **changes})


def success(item, text='Ola', **changes) -> TranslationProviderResult:
    return TranslationProviderResult(item.request_id, ProviderStatus.SUCCEEDED, FAKE_PRODUCER, DeliveryState.COMPLETED,
                                     **{'text': text, **changes})


def failure(item, code='fake_failure', **changes) -> TranslationProviderResult:
    return TranslationProviderResult(item.request_id, ProviderStatus.FAILED, FAKE_PRODUCER, DeliveryState.UNKNOWN,
                                     failure=JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, code, True), **changes)


class FakeProvider:
    def __init__(self, behavior=None, on_close=None) -> None:
        self.behavior = behavior or success
        self.on_close = on_close
        self.requests = []
        self.closed = 0

    def execute(self, item):
        self.requests.append(item)
        return self.behavior(item)

    def close(self) -> None:
        self.closed += 1
        if self.on_close is not None:
            self.on_close()


class FakeFactory:
    adapter_id = 'fake_engine'
    adapter_version = 3
    supported_config_versions = frozenset({1, 2})

    def __init__(self, make=None) -> None:
        self.make = make or FakeProvider
        self.created = []

    def create(self, instance):
        provider = self.make()
        self.created.append((instance, provider))
        return provider


def fake_instance(**changes) -> ProviderInstance:
    return ProviderInstance(**{'instance_id': 'fake_local', 'adapter_id': 'fake_engine', 'adapter_version': 3,
                               'config_version': 1, **changes})


class FakeSession:
    def __init__(self, responses=(), get_responses=()) -> None:
        self.responses = list(responses)
        self.get_responses = list(get_responses)
        self.posts = []
        self.gets = []
        self.closed = 0

    def post(self, url, *, json, timeout, allow_redirects=False):
        assert allow_redirects is False
        self.posts.append((url, json, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, *, timeout, allow_redirects=False):
        assert allow_redirects is False
        self.gets.append((url, timeout))
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed += 1


def http_response(status_code=200, body='{"translatedText": "Ola"}') -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode('utf-8')
    response.url = 'http://localhost:5000/translate'
    return response
