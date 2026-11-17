"""A fake model behind the LLM core's ``ModelPort``, and the facts a fake adapter declares.

The fake reads the rendered JSON data like a real model would and answers through the structured schema,
so tests drive the real core: prompt rendering, batching, parsing and validation all run.
"""
from __future__ import annotations

import json
from uuid import UUID

from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.capabilities import Capability, CapabilityFact, EvidenceSource, ProviderDescriptor, SupportState
from ayvu.providers.llm import (
    TRANSPORT_CAPABILITIES, FinishReason, ModelCallError, ModelResponse, TransportReason, llm_snapshot,
)
from ayvu.providers.ports import TranslationProviderRequest

LLM_PRODUCER = ProducerIdentity("fake_llm", "fake-model@1", "fake_llm_adapter@1", "llm_core@1")
FAKE_DESCRIPTOR = ProviderDescriptor("fake_llm", 1, "fake_llm_port", 1, (1,), (1,))
FAKE_TRANSPORT_FACTS = tuple(
    CapabilityFact(item, SupportState.SUPPORTED if item is Capability.MODEL_IDENTITY else SupportState.UNSUPPORTED,
                   EvidenceSource.ADAPTER_CODE)
    for item in sorted(TRANSPORT_CAPABILITIES, key=lambda capability: capability.value))


def fake_snapshot(now):
    return llm_snapshot(FAKE_DESCRIPTOR, FAKE_TRANSPORT_FACTS, (), now)


def unit_request(index: int, text: str, source="en", target="pt", **changes) -> TranslationProviderRequest:
    return TranslationProviderRequest(str(UUID(int=index + 1)), text, source, target, **changes)


def answer(items, *, finish=FinishReason.STOP) -> ModelResponse:
    """``items`` is ``[(id, text), ...]`` in the order the model writes them."""
    return ModelResponse(finish, json.dumps({"translations": [{"id": key, "text": text} for key, text in items]}))


def segments_of(request) -> list[dict]:
    return json.loads(request.user)["segments"]


def rate_limited(seconds=None) -> ModelCallError:
    return ModelCallError(TransportReason.HTTP, status_code=429, retry_after_seconds=seconds)


def default_translation(text: str) -> str:
    return f"PT[{text}]"


class FakeModel:
    """Each call takes the next scripted step, then answers every segment with ``translate``.

    A step is an exception to raise, a ``ModelResponse`` to return, or a callable taking the segments.
    """

    def __init__(self, *script, translate=default_translation) -> None:
        self.script = list(script)
        self.translate = translate
        self.requests = []
        self.closed = 0

    def complete(self, request):
        self.requests.append(request)
        segments = segments_of(request)
        step = self.script.pop(0) if self.script else None
        if step is None:
            return answer([(item["id"], self.translate(item["text"])) for item in segments])
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ModelResponse):
            return step
        return step(segments)

    def close(self) -> None:
        self.closed += 1

    def batches(self) -> list[list[str]]:
        return [[item["id"] for item in segments_of(request)] for request in self.requests]
