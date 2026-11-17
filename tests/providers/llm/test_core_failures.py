"""The core over a fake model: refusals, transport failures, 429 with retry-after, and pre-request refusals."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ayvu.content.model import Constraint
from ayvu.jobs.execution import CancellationSource
from ayvu.kernel import JobFailureFamily
from ayvu.providers.llm import (
    LLM_SAFE_POINTS, FinishReason, LlmSettings, LlmTranslationProvider, ModelCallError, ModelResponse, TransportReason,
)
from ayvu.providers.ports import (
    ConstraintsFeature, ContextFeature, ContextItem, DeliveryState, ProviderRoute, ProviderStatus, Requirement,
    result_diagnostics,
)
from tests.support.llm_fakes import LLM_PRODUCER, FakeModel, rate_limited, segments_of, unit_request


def provider(model, sleeps=None, **settings):
    return LlmTranslationProvider(model, LLM_PRODUCER, settings=LlmSettings(**settings),
                                  sleep=(sleeps.append if sleeps is not None else lambda _: None))


def outcome(result):
    failure = result.failure
    return (result.status, failure.family, failure.code, failure.retryable, result.delivery)


def test_a_refusal_or_safety_stop_is_a_non_retryable_refusal_for_the_whole_batch():
    model = FakeModel(ModelResponse(FinishReason.REFUSAL))
    results = provider(model).execute_batch((unit_request(0, "One"), unit_request(1, "Two")))
    assert [outcome(item) for item in results] == [(ProviderStatus.FAILED, JobFailureFamily.PROVIDER_PERMANENT,
                                                   "provider_refusal", False, DeliveryState.COMPLETED)] * 2
    assert len(model.requests) == 1


def test_a_429_honours_retry_after_then_succeeds():
    sleeps = []
    model = FakeModel(rate_limited(2.5))
    result = provider(model, sleeps).execute(unit_request(0, "Hello"))
    assert sleeps == [2.5]
    assert (result.status, result.text) == (ProviderStatus.SUCCEEDED, "PT[Hello]")
    assert len(model.requests) == 2


def test_a_429_still_limited_after_the_retries_is_rate_limited_with_its_retry_after():
    sleeps = []
    model = FakeModel(rate_limited(2), rate_limited(3))
    result = provider(model, sleeps).execute(unit_request(0, "Hello"))
    assert sleeps == [2]
    assert outcome(result) == (ProviderStatus.FAILED, JobFailureFamily.PROVIDER_TRANSIENT, "provider_rate_limited",
                               True, DeliveryState.COMPLETED)
    assert result.failure.diagnostics == (("retry_after_ms", 3000),)


@pytest.mark.parametrize("seconds,settings", [(None, {}), (120, {}), (1, {"rate_limit_retries": 0})])
def test_a_429_is_not_waited_out_without_a_bounded_retry_after(seconds, settings):
    sleeps = []
    model = FakeModel(rate_limited(seconds))
    result = provider(model, sleeps, **settings).execute(unit_request(0, "Hello"))
    assert sleeps == [] and len(model.requests) == 1
    assert result.failure.code == "provider_rate_limited" and result.failure.retryable


@pytest.mark.parametrize("error,expected", [
    (ModelCallError(TransportReason.TIMEOUT),
     (JobFailureFamily.PROVIDER_TRANSIENT, "provider_timeout", True, DeliveryState.UNKNOWN)),
    (ModelCallError(TransportReason.CONNECTION),
     (JobFailureFamily.PROVIDER_TRANSIENT, "provider_unavailable", True, DeliveryState.UNKNOWN)),
    (ModelCallError(TransportReason.REQUEST),
     (JobFailureFamily.PROVIDER_TRANSIENT, "provider_request_failed", True, DeliveryState.UNKNOWN)),
    (ModelCallError(TransportReason.HTTP, status_code=503),
     (JobFailureFamily.PROVIDER_TRANSIENT, "provider_server_error", True, DeliveryState.COMPLETED)),
    (ModelCallError(TransportReason.HTTP, status_code=401),
     (JobFailureFamily.PROVIDER_PERMANENT, "provider_unauthorized", False, DeliveryState.COMPLETED)),
    (ModelCallError(TransportReason.HTTP, status_code=400),
     (JobFailureFamily.PROVIDER_PERMANENT, "provider_rejected", False, DeliveryState.COMPLETED)),
    (ModelCallError(TransportReason.POLICY),
     (JobFailureFamily.POLICY, "provider_policy_refused", False, DeliveryState.NOT_SENT)),
], ids=["timeout", "connection", "request", "5xx", "401", "400", "policy"])
def test_transport_failures_map_to_the_existing_families(error, expected):
    result = provider(FakeModel(error)).execute(unit_request(0, "Hello"))
    assert outcome(result)[1:] == expected


def test_an_adapter_exception_is_reduced_to_its_type_without_its_message():
    secret = "http://user:hunter2@model.example/v1 Hello book text"
    result = provider(FakeModel(RuntimeError(secret))).execute(unit_request(0, "Hello"))
    assert outcome(result) == (ProviderStatus.FAILED, JobFailureFamily.INTERNAL, "unexpected_error", False,
                               DeliveryState.UNKNOWN)
    assert "hunter2" not in repr(result) + repr(result_diagnostics(result))


def test_cancellation_before_the_request_sends_nothing():
    source = CancellationSource(LLM_SAFE_POINTS)
    source.request("user_requested", datetime(2026, 11, 17, 12, tzinfo=UTC))
    model = FakeModel()
    result = provider(model).execute(unit_request(0, "Hello", cancellation=source.token))
    assert outcome(result) == (ProviderStatus.CANCELLED, JobFailureFamily.CANCELLED, "cancelled_before_request", False,
                               DeliveryState.NOT_SENT)
    assert model.requests == []


def test_a_required_feature_the_core_cannot_honour_fails_before_any_call():
    model = FakeModel()
    required = ConstraintsFeature((Constraint("max_chars", 10),), Requirement.REQUIRED)
    advisory = ConstraintsFeature((Constraint("max_chars", 10),), Requirement.ADVISORY)
    refused = provider(model).execute(unit_request(0, "Hello", features=(required,)))
    assert outcome(refused) == (ProviderStatus.FAILED, JobFailureFamily.CAPABILITY, "unsupported_required_feature",
                                False, DeliveryState.NOT_SENT)
    assert model.requests == []
    warned = provider(model).execute(unit_request(1, "Hello", features=(advisory,)))
    assert warned.status is ProviderStatus.SUCCEEDED and [item.code for item in warned.warnings] == ["ignored_constraints"]


def test_a_pivot_route_is_refused_because_a_model_translates_directly():
    model = FakeModel()
    result = provider(model).execute(unit_request(0, "Hello", route=ProviderRoute("en", "pt", "es")))
    assert result.failure.code == "route_conflict" and model.requests == []


def test_negotiated_context_reaches_the_prompt_as_reference_only():
    model = FakeModel()
    context = ContextFeature((ContextItem("translation_context", "The previous paragraph."),), Requirement.REQUIRED)
    result = provider(model).execute(unit_request(0, "Hello", features=(context,)))
    assert result.status is ProviderStatus.SUCCEEDED and result.warnings == ()
    assert segments_of(model.requests[0]) == [{"id": "s0", "text": "Hello", "context": [
        {"role": "translation_context", "text": "The previous paragraph."}]}]


def test_close_closes_the_model():
    model = FakeModel()
    provider(model).close()
    assert model.closed == 1
