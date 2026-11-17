"""The provider-neutral LLM translation core: prompt, batching, structured output, validation and failures.

A model adapter implements ``ModelPort`` and declares its transport facts; composition wires both.
The core holds no vendor, network or credential.
"""
from __future__ import annotations

from .batching import estimate_tokens, plan_batches
from .core import LLM_SAFE_POINTS, LlmTranslationProvider
from .facts import CORE_FACTS, TRANSPORT_CAPABILITIES, llm_snapshot
from .model import (
    LLM_CORE_VERSION, BatchPolicy, DecodingSettings, FinishReason, GlossaryHint, GlossaryRule, LlmCoreError,
    ModelCallError, ModelPort, ModelRequest, ModelResponse, TransportReason,
)
from .output import StructuredOutputError, parse_translations
from .prompt import DEFAULT_PROMPT, OUTPUT_SCHEMA, PROMPT_STRATEGY, PROMPT_V1, PromptSegment, PromptTemplate, render_prompt
from .settings import PROMPT_REVISION_NAMESPACE, PROMPT_REVISION_OWNER, LlmSemantics, LlmSettings

__all__ = [
    "BatchPolicy", "CORE_FACTS", "DEFAULT_PROMPT", "DecodingSettings", "FinishReason", "GlossaryHint", "GlossaryRule",
    "LLM_CORE_VERSION", "LLM_SAFE_POINTS", "LlmCoreError", "LlmSemantics", "LlmSettings", "LlmTranslationProvider",
    "ModelCallError", "ModelPort", "ModelRequest", "ModelResponse", "OUTPUT_SCHEMA", "PROMPT_REVISION_NAMESPACE",
    "PROMPT_REVISION_OWNER", "PROMPT_STRATEGY", "PROMPT_V1", "PromptSegment", "PromptTemplate",
    "StructuredOutputError", "TRANSPORT_CAPABILITIES", "TransportReason", "estimate_tokens", "llm_snapshot",
    "parse_translations", "plan_batches", "render_prompt",
]
