"""The provider-neutral LLM translation core, behind the rich provider request and result.

A model adapter only maps transport (``ModelPort``). Prompting, batching, structured-output
validation, the strict result check and the failure mapping happen here, once, for every vendor.
Results map back to request ids through segment ids, never through positions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ...content import protected_content_is_intact
from ...kernel import JobFailure, JobFailureFamily, JobWarning, normalize_failure
from ...provenance import ProducerIdentity
from ..ports import (
    ContextFeature, DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderRequest,
    TranslationProviderResult,
)
from ..service import FeatureDecision, decide_features, failed_result
from .batching import estimate_tokens, plan_batches
from .facts import CORE_FEATURES
from .failures import (
    EMPTY_RESULT, OUTPUT_INVALID, OUTPUT_TRUNCATED, PLACEHOLDER_MISMATCH, REFUSAL, retry_after_ms, transport_failure,
)
from .model import FinishReason, GlossaryHint, LlmCoreError, ModelCallError, ModelPort, ModelResponse
from .output import StructuredOutputError, parse_translations
from .prompt import PromptSegment, render_prompt, render_user
from .settings import LlmSettings

LLM_SAFE_POINT = "provider_before_request"
LLM_SAFE_POINTS = (LLM_SAFE_POINT,)
MAX_BATCH_REQUESTS = 256
_Outcome = ModelResponse | tuple[JobFailure, DeliveryState]


@dataclass(frozen=True, repr=False)
class _Unit:
    request: TranslationProviderRequest
    segment: PromptSegment
    route: ProviderRoute
    warnings: tuple[JobWarning, ...]


def _language(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()


def _context(request: TranslationProviderRequest) -> tuple[tuple[str, str], ...]:
    feature = request.feature(ContextFeature)
    return () if feature is None else tuple((item.role, item.text) for item in feature.items)


def _outer(text: str) -> tuple[str, str]:
    core = text.strip()
    if not core:
        return text, ""
    start = text.index(core)
    return text[:start], text[start + len(core):]


class LlmTranslationProvider:
    """A ``TranslationProvider`` over a model port. ``execute_batch`` sends several units per request."""

    def __init__(self, model: ModelPort, producer: ProducerIdentity, *, settings: LlmSettings = LlmSettings(),
                 glossary: tuple[GlossaryHint, ...] = (), sleep: Callable[[float], None] = time.sleep) -> None:
        if not callable(getattr(model, "complete", None)) or not callable(getattr(model, "close", None)):
            raise LlmCoreError("invalid_model_port")
        if type(producer) is not ProducerIdentity or type(settings) is not LlmSettings:
            raise LlmCoreError("invalid_llm_provider")
        if type(glossary) is not tuple or any(type(item) is not GlossaryHint for item in glossary):
            raise LlmCoreError("invalid_glossary_hint")
        self._model, self._producer, self._settings = model, producer, settings
        self._glossary, self._sleep = glossary, sleep

    def __repr__(self) -> str:
        return f"LlmTranslationProvider(prompt_revision={self._settings.prompt.revision})"

    def close(self) -> None:
        self._model.close()

    def execute(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        return self.execute_batch((request,))[0]

    def execute_batch(self, requests: tuple[TranslationProviderRequest, ...]) -> tuple[TranslationProviderResult, ...]:
        """One result per request, in the order given. Units of one route only: a batch is one prompt."""
        if (type(requests) is not tuple or not 1 <= len(requests) <= MAX_BATCH_REQUESTS
                or any(type(item) is not TranslationProviderRequest for item in requests)):
            raise LlmCoreError("invalid_batch")
        if len({item.request_id for item in requests}) != len(requests):
            raise LlmCoreError("duplicate_request")
        if len({(item.source_locale, item.target_locale) for item in requests}) != 1:
            raise LlmCoreError("mixed_languages")
        results: dict[str, TranslationProviderResult] = {}
        units: list[_Unit] = []
        for request in requests:
            route = ProviderRoute(request.source_locale, request.target_locale)
            decision = decide_features(request, CORE_FEATURES)
            refusal = self._refusal(request, route, decision)
            if refusal is not None:
                results[request.request_id] = refusal
                continue
            segment = PromptSegment(f"s{len(units)}", request.text, _context(request))
            units.append(_Unit(request, segment, route, decision.ignored_advisory))
        if units:
            source, target = units[0].route.source, units[0].route.target
            policy = self._settings.batch
            overhead = estimate_tokens(self._settings.prompt.system + render_user(source, target, (), self._glossary),
                                       policy)
            for group in plan_batches(tuple(unit.segment for unit in units), policy, overhead):
                results.update(self._run(tuple(units[index] for index in group)))
        return tuple(results[item.request_id] for item in requests)

    def _failed(self, unit: _Unit, failure: JobFailure, delivery: DeliveryState) -> TranslationProviderResult:
        return failed_result(unit.request, failure, producer=self._producer, delivery=delivery,
                             warnings=unit.warnings, route=unit.route)

    def _refusal(self, request: TranslationProviderRequest, route: ProviderRoute,
                 decision: FeatureDecision) -> TranslationProviderResult | None:
        """Everything decided before any model call. A model translates directly, so no pivot route."""
        def refuse(code: str) -> TranslationProviderResult:
            return failed_result(request, JobFailure(JobFailureFamily.CAPABILITY, code), producer=self._producer,
                                 delivery=DeliveryState.NOT_SENT, warnings=decision.ignored_advisory, route=route)

        if decision.unsupported_required:
            return refuse("unsupported_required_feature")
        if request.route is not None and request.route != route:
            return refuse("route_conflict")
        return None

    def _run(self, units: tuple[_Unit, ...]) -> dict[str, TranslationProviderResult]:
        """One model call for these units. A structural failure is retried once per unit, alone."""
        outcome = self._call(units)
        if type(outcome) is tuple:
            failure, delivery = outcome
            return {unit.request.request_id: self._failed(unit, failure, delivery) for unit in units}
        if outcome.finish is FinishReason.REFUSAL:
            return {unit.request.request_id: self._failed(unit, REFUSAL, DeliveryState.COMPLETED) for unit in units}
        try:
            if outcome.finish is FinishReason.LENGTH:
                raise StructuredOutputError("truncated")
            texts = parse_translations(outcome.text, tuple(unit.segment.id for unit in units))
        except StructuredOutputError:
            if len(units) > 1:
                results: dict[str, TranslationProviderResult] = {}
                for unit in units:
                    results.update(self._run((unit,)))
                return results
            failure = OUTPUT_TRUNCATED if outcome.finish is FinishReason.LENGTH else OUTPUT_INVALID
            return {units[0].request.request_id: self._failed(units[0], failure, DeliveryState.COMPLETED)}
        return {unit.request.request_id: self._checked(unit, texts[unit.segment.id]) for unit in units}

    def _call(self, units: tuple[_Unit, ...]) -> _Outcome:
        """The model call, waiting out a 429 within the configured bounds. Cancellation is seen before each try."""
        first = units[0].route
        prompt = render_prompt(self._settings.prompt, first.source, first.target,
                               tuple(unit.segment for unit in units), self._settings.decoding, self._glossary)
        attempts = 0
        while True:
            if any(unit.request.cancellation is not None
                   and unit.request.cancellation.checkpoint(LLM_SAFE_POINT) is not None for unit in units):
                return JobFailure(JobFailureFamily.CANCELLED, "cancelled_before_request"), DeliveryState.NOT_SENT
            try:
                response = self._model.complete(prompt)
            except ModelCallError as error:
                wait = retry_after_ms(error)
                if (error.status_code == 429 and wait is not None and attempts < self._settings.rate_limit_retries
                        and wait <= self._settings.max_retry_wait_ms):
                    attempts += 1
                    self._sleep(wait / 1000)
                    continue
                return transport_failure(error)
            except Exception as error:
                # An adapter's own exception may quote a URL or the book; only its type is kept.
                return normalize_failure(error), DeliveryState.UNKNOWN
            if type(response) is not ModelResponse:
                return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "malformed_response"), DeliveryState.COMPLETED
            return response

    def _checked(self, unit: _Unit, text: str) -> TranslationProviderResult:
        """The strict result check every provider result passes, plus language sanity."""
        request = unit.request
        core = text.strip()
        if not core:
            return self._failed(unit, EMPTY_RESULT, DeliveryState.COMPLETED)
        # A model drops the outer whitespace the pipeline relies on to join chunks; it comes back from the source.
        lead, trail = _outer(request.text)
        translated = lead + core + trail
        if not protected_content_is_intact(request.text, translated):
            return self._failed(unit, PLACEHOLDER_MISMATCH, DeliveryState.COMPLETED)
        warnings = unit.warnings
        if (_language(request.source_locale) != _language(request.target_locale) and core == request.text.strip()
                and any(character.isalpha() for character in core)):
            warnings = (*warnings, JobWarning("untranslated_result"))
        return TranslationProviderResult(
            request.request_id, ProviderStatus.SUCCEEDED, self._producer, DeliveryState.COMPLETED, text=translated,
            route=unit.route, warnings=warnings, usage=ProviderUsage(characters_submitted=len(request.text)))
