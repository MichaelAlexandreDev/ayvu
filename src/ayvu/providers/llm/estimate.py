"""How many tokens a model call will use, before it is made, and the ports a run's cost cap plugs into.

Without a tokenizer, input tokens are the rendered request's characters divided by the batch policy's
``chars_per_token``, as batching already estimates them; such an estimate is labelled approximate. A
provider's own counter (``TokenCounter``) replaces that count when one is wired. Output tokens are
always estimated: the answer's characters times ``OUTPUT_TOKEN_FACTOR``. The worst case, which a cost
cap reserves, is the estimated input plus the request's full ``max_output_tokens``.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from ..ports import ProviderUsage
from .batching import estimate_tokens
from .model import BatchPolicy, GlossaryHint, ModelRequest
from .prompt import PromptSegment, render_prompt
from .settings import LlmSettings

# A translation is assumed to be a fifth longer than its source: a declared factor, not a measurement.
OUTPUT_TOKEN_FACTOR = Fraction(6, 5)


class TokenCounter(Protocol):
    """A provider's exact count of a request's input tokens. It may send the request's text to the provider,
    so composition wires it only where that egress is already consented."""

    def count_input_tokens(self, request: ModelRequest) -> int: ...


class CostMeter(Protocol):
    """A run's shared cost cap. ``reserve`` returns ``None`` when the call does not fit; it is then not sent.

    Every reservation is settled once, with what the call reported; ``None`` means nothing is known of it.
    """

    def reserve(self, input_tokens: int, max_output_tokens: int) -> object | None: ...
    def settle(self, reservation: object, spent: ProviderUsage | None) -> None: ...


@dataclass(frozen=True)
class CallEstimate:
    input_tokens: int
    output_tokens: int
    worst_output_tokens: int
    approximate: bool


def input_estimate(request: ModelRequest, policy: BatchPolicy) -> int:
    """The approximate input tokens of a rendered request: its instructions and its data, as batching counts them."""
    return estimate_tokens(request.system + request.user, policy)


def _answer_chars(segments: tuple[PromptSegment, ...]) -> int:
    answer = {"translations": [{"id": segment.id, "text": segment.text} for segment in segments]}
    return len(json.dumps(answer, ensure_ascii=False, separators=(",", ":")))


def estimate_call(request: ModelRequest, segments: tuple[PromptSegment, ...], policy: BatchPolicy,
                  counter: TokenCounter | None = None) -> CallEstimate:
    counted = None if counter is None else counter.count_input_tokens(request)
    if counted is not None and (type(counted) is not int or counted < 0):
        counted = None
    output = math.ceil(Fraction(_answer_chars(segments), policy.chars_per_token) * OUTPUT_TOKEN_FACTOR)
    return CallEstimate(input_estimate(request, policy) if counted is None else counted, output,
                        request.decoding.max_output_tokens, counted is None)


class LlmUsageEstimator:
    """Sums the estimate of each request a run would send: one unit of text per request, as the rich runtime does."""

    def __init__(self, settings: LlmSettings, source: str, target: str, glossary: tuple[GlossaryHint, ...] = (), *,
                 counter: TokenCounter | None = None) -> None:
        self._settings, self._source, self._target = settings, source, target
        self._glossary, self._counter = glossary, counter
        self.calls = self.characters = self.input_tokens = self.output_tokens = self.worst_output_tokens = 0
        self.approximate = counter is None

    def __repr__(self) -> str:
        return f"LlmUsageEstimator(calls={self.calls})"

    def add(self, text: str) -> None:
        segments = (PromptSegment("s0", text),)
        request = render_prompt(self._settings.prompt, self._source, self._target, segments,
                                self._settings.decoding, self._glossary)
        estimate = estimate_call(request, segments, self._settings.batch, self._counter)
        self.calls += 1
        self.characters += len(text)
        self.input_tokens += estimate.input_tokens
        self.output_tokens += estimate.output_tokens
        self.worst_output_tokens += estimate.worst_output_tokens
        self.approximate = self.approximate or estimate.approximate
