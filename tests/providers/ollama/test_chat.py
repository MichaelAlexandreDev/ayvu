"""The LLM core over Ollama's chat port and a fake loopback server: answers, failures and cancellation (#376)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from ayvu.jobs.execution import CancellationSource
from ayvu.kernel import JobFailureFamily
from ayvu.network import resolve_network_policy
from ayvu.network.http import NetworkAuthorization
from ayvu.providers.llm import LLM_SAFE_POINTS, OUTPUT_SCHEMA, PROMPT_V1, DecodingSettings, LlmSettings, LlmTranslationProvider
from ayvu.providers.ollama.chat import OllamaChatModel, chat_options
from ayvu.providers.ollama.client import InstalledModel, OllamaClient
from ayvu.providers.ollama.discovery import ollama_producer
from ayvu.providers.ports import DeliveryState, ProviderStatus
from tests.support.llm_fakes import unit_request
from tests.support.ollama_fakes import DIGEST_A, MODEL, chat_answer, fake_ollama, reply, slow  # noqa: F401


def provider(url, *, timeout=5.0, settings=LlmSettings(), sleep=lambda _: None):
    client = OllamaClient(NetworkAuthorization(resolve_network_policy("loopback", url)), timeout=timeout)
    return LlmTranslationProvider(OllamaChatModel(client, MODEL), ollama_producer(InstalledModel(MODEL, DIGEST_A)),
                                  settings=settings, sleep=sleep)


def outcome(result):
    failure = result.failure
    return result.status, failure.family if failure else None, failure.code if failure else None, result.delivery


def test_a_structured_answer_is_asked_for_with_the_schema_and_mapped_by_id(fake_ollama):
    result = provider(fake_ollama.url).execute(unit_request(0, "Hello reader."))
    assert result.status is ProviderStatus.SUCCEEDED and result.text == "PT[Hello reader.]"
    assert result.producer.model == f"{MODEL}@{DIGEST_A}"
    body = fake_ollama.chat_bodies[0]
    assert body["model"] == MODEL and body["stream"] is False and body["format"] == OUTPUT_SCHEMA
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == PROMPT_V1.system
    assert json.loads(body["messages"][1]["content"])["segments"] == [{"id": "s0", "text": "Hello reader."}]
    assert body["options"] == {"temperature": 0.0, "num_predict": 4096}


def test_every_decoding_value_reaches_the_server():
    decoding = DecodingSettings(temperature="0.3", top_p="0.9", max_output_tokens=512, seed=7)
    assert chat_options(decoding) == {"temperature": 0.3, "top_p": 0.9, "num_predict": 512, "seed": 7}


def test_an_invalid_answer_is_retried_alone_then_fails_as_invalid_output(fake_ollama):
    fake_ollama.script = [reply(200, chat_answer("not json")), reply(200, chat_answer('{"translations": []}'))]
    batch = provider(fake_ollama.url).execute_batch((unit_request(0, "One."), unit_request(1, "Two.")))
    # The batch fails as a whole, then each unit goes alone: the first still invalid, the second answered.
    assert outcome(batch[0]) == (ProviderStatus.FAILED, JobFailureFamily.PROVIDER_PERMANENT, "provider_output_invalid",
                                 DeliveryState.COMPLETED)
    assert batch[1].status is ProviderStatus.SUCCEEDED and batch[1].text == "PT[Two.]"
    assert fake_ollama.chat_count() == 3


def test_a_cut_off_answer_fails_as_truncated(fake_ollama):
    fake_ollama.script = [reply(200, chat_answer('{"translations": [', done_reason="length"))]
    assert outcome(provider(fake_ollama.url).execute(unit_request(0, "Hello."))) == (
        ProviderStatus.FAILED, JobFailureFamily.PROVIDER_PERMANENT, "provider_output_truncated", DeliveryState.COMPLETED)


def test_a_body_without_a_message_is_a_malformed_response(fake_ollama):
    fake_ollama.script = [reply(200, {"done": True})]
    assert outcome(provider(fake_ollama.url).execute(unit_request(0, "Hello.")))[1:3] == (
        JobFailureFamily.PROVIDER_PERMANENT, "malformed_response")


def test_a_timeout_is_transient_with_an_unknown_delivery(fake_ollama):
    fake_ollama.script = [slow(1.0)]
    assert outcome(provider(fake_ollama.url, timeout=0.2).execute(unit_request(0, "Hello."))) == (
        ProviderStatus.FAILED, JobFailureFamily.PROVIDER_TRANSIENT, "provider_timeout", DeliveryState.UNKNOWN)


def test_a_429_with_retry_after_is_waited_out_by_the_core(fake_ollama):
    fake_ollama.script = [reply(429, headers={"Retry-After": "2"})]
    waits = []
    result = provider(fake_ollama.url, sleep=waits.append).execute(unit_request(0, "Hello."))
    assert result.status is ProviderStatus.SUCCEEDED and waits == [2.0] and fake_ollama.chat_count() == 2


def test_cancellation_before_the_request_sends_nothing(fake_ollama):
    source = CancellationSource(LLM_SAFE_POINTS)
    source.request("user_requested", datetime(2026, 11, 17, 12, tzinfo=UTC))
    result = provider(fake_ollama.url).execute(unit_request(0, "Hello.", cancellation=source.token))
    assert outcome(result) == (ProviderStatus.CANCELLED, JobFailureFamily.CANCELLED, "cancelled_before_request",
                               DeliveryState.NOT_SENT)
    assert fake_ollama.requests == []
