"""Values of the LLM translation core and the port a model adapter implements.

Nothing here names a vendor, holds a credential or performs I/O. A prompt and a model's answer
carry the book, so every text-bearing value has a redacted representation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from ..ports import ProviderError

LLM_CORE_VERSION = 1
MAX_GLOSSARY_TERM_CHARS = 512
MAX_GENERATION_INTEGER = 2**31 - 1


class LlmCoreError(ProviderError):
    """A bounded error: messages never include caller data."""


class GlossaryRule(StrEnum):
    TRANSLATE = "translate"
    PRESERVE = "preserve"
    FORBIDDEN = "forbidden"


def _bounded_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_GLOSSARY_TERM_CHARS


@dataclass(frozen=True, repr=False)
class GlossaryHint:
    """One active glossary rule, given to the model as a hint. The glossary still runs after translation."""

    rule: GlossaryRule
    term: str
    translation: str | None = None

    def __post_init__(self) -> None:
        if type(self.rule) is not GlossaryRule or not _bounded_text(self.term):
            raise LlmCoreError("invalid_glossary_hint")
        if (self.rule is GlossaryRule.TRANSLATE) != (self.translation is not None):
            raise LlmCoreError("invalid_glossary_hint")
        if self.translation is not None and not _bounded_text(self.translation):
            raise LlmCoreError("invalid_glossary_hint")

    def __repr__(self) -> str:
        return f"GlossaryHint(rule={self.rule.value!r})"


def _decimal(value: object, low: Decimal, high: Decimal) -> str:
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if (number is None or not number.is_finite() or not low <= number <= high
            or number.normalize().as_tuple().exponent < -6):
        raise LlmCoreError("invalid_decoding_settings")
    return format(number.normalize() if number else Decimal(0), "f")


def _positive(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_GENERATION_INTEGER


@dataclass(frozen=True)
class DecodingSettings:
    """How the model samples. Every value can change a translation, so all of them are in its identity.

    Decimals are strings, so a value is exact and has one canonical form.
    """

    temperature: str = "0"
    top_p: str | None = None
    max_output_tokens: int = 4096
    seed: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "temperature", _decimal(self.temperature, Decimal(0), Decimal(2)))
        if self.top_p is not None:
            object.__setattr__(self, "top_p", _decimal(self.top_p, Decimal(0), Decimal(1)))
        if not _positive(self.max_output_tokens):
            raise LlmCoreError("invalid_decoding_settings")
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed <= MAX_GENERATION_INTEGER):
            raise LlmCoreError("invalid_decoding_settings")

    def parameters(self) -> tuple[tuple[str, str, object], ...]:
        """``(key, kind, value)``. An unset optional value is absent, which differs from any set value."""
        values: list[tuple[str, str, object]] = [
            ("decoding_max_output_tokens", "integer", self.max_output_tokens),
            ("decoding_temperature", "decimal", self.temperature),
        ]
        if self.top_p is not None:
            values.append(("decoding_top_p", "decimal", self.top_p))
        if self.seed is not None:
            values.append(("decoding_seed", "integer", self.seed))
        return tuple(values)


@dataclass(frozen=True)
class BatchPolicy:
    """Several units of one document per request, bounded by a units cap and an estimated token budget.

    No tokenizer is assumed: a request's tokens are estimated as its characters divided by
    ``chars_per_token``, rounded up, which is deliberately conservative for most languages.
    """

    max_units: int = 16
    token_budget: int = 2048
    chars_per_token: int = 3

    def __post_init__(self) -> None:
        if not all(_positive(value) for value in (self.max_units, self.token_budget, self.chars_per_token)):
            raise LlmCoreError("invalid_batch_policy")

    def parameters(self) -> tuple[tuple[str, str, object], ...]:
        return (("batch_chars_per_token", "integer", self.chars_per_token),
                ("batch_max_units", "integer", self.max_units),
                ("batch_token_budget", "integer", self.token_budget))


@dataclass(frozen=True, repr=False)
class ModelRequest:
    """One call to a model: shared instructions, the rendered data and the schema the answer must follow."""

    system: str
    user: str
    response_schema_json: str
    decoding: DecodingSettings

    def __post_init__(self) -> None:
        if (not isinstance(self.system, str) or not isinstance(self.user, str)
                or not isinstance(self.response_schema_json, str) or type(self.decoding) is not DecodingSettings):
            raise LlmCoreError("invalid_model_request")

    @property
    def response_schema(self) -> dict:
        """A fresh copy, so an adapter cannot change the schema the core validates against."""
        return json.loads(self.response_schema_json)

    def __repr__(self) -> str:
        return "ModelRequest(<redacted>)"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    REFUSAL = "refusal"


def _token_count(value: object) -> bool:
    return value is None or (type(value) is int and 0 <= value < 2**63)


@dataclass(frozen=True, repr=False)
class ModelResponse:
    """What a model answered. A refusal or a safety stop is ``REFUSAL``; a cut-off answer is ``LENGTH``."""

    finish: FinishReason
    text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if type(self.finish) is not FinishReason or not (self.text is None or isinstance(self.text, str)):
            raise LlmCoreError("invalid_model_response")
        if self.finish is FinishReason.STOP and self.text is None:
            raise LlmCoreError("invalid_model_response")
        if not _token_count(self.input_tokens) or not _token_count(self.output_tokens):
            raise LlmCoreError("invalid_model_response")

    def __repr__(self) -> str:
        return f"ModelResponse(finish={self.finish.value!r})"


class TransportReason(StrEnum):
    POLICY = "policy"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    REQUEST = "request"
    HTTP = "http"


class ModelCallError(Exception):
    """A failed call, described only by typed attributes. Its message is never read: it may quote a URL."""

    def __init__(self, reason: TransportReason, *, status_code: int | None = None,
                 retry_after_seconds: float | None = None) -> None:
        if type(reason) is not TransportReason:
            raise LlmCoreError("invalid_transport_error")
        if (reason is TransportReason.HTTP) != (status_code is not None):
            raise LlmCoreError("invalid_transport_error")
        if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
            raise LlmCoreError("invalid_transport_error")
        if retry_after_seconds is not None and (type(retry_after_seconds) not in (int, float)
                                                or not 0 <= retry_after_seconds < 2**31):
            raise LlmCoreError("invalid_transport_error")
        super().__init__(reason.value)
        self.reason = reason
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ModelPort(Protocol):
    """What a model adapter maps to its transport. It never retries a 429 itself: the core does."""

    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def close(self) -> None: ...
