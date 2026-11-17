"""The settings of one LLM translation run, and the part of them that is semantic cache identity.

Providers cannot name the cache's types, so ``LlmSemantics`` holds plain values. Composition turns
them into the plan's prompt revision and generation parameters, which the v2 fingerprint covers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import LLM_CORE_VERSION, BatchPolicy, DecodingSettings, LlmCoreError
from .prompt import DEFAULT_PROMPT, OUTPUT_SCHEMA_REVISION, PROMPT_STRATEGY, PromptTemplate

PROMPT_REVISION_OWNER = "ayvu"
PROMPT_REVISION_NAMESPACE = "llm_prompt"
PROMPT_SNAPSHOT_KIND = "llm_prompt"
MAX_RETRY_WAIT_MS = 10 * 60 * 1000


@dataclass(frozen=True)
class LlmSemantics:
    """Everything of a run that can change an output, as plain values."""

    prompt_strategy: str
    prompt_version: int
    prompt_value: str
    prompt_snapshot: str
    generation_parameters: tuple[tuple[str, str, object], ...]


@dataclass(frozen=True, repr=False)
class LlmSettings:
    """``rate_limit_retries`` and ``max_retry_wait_ms`` are operational, so they stay out of the identity."""

    prompt: PromptTemplate = DEFAULT_PROMPT
    batch: BatchPolicy = BatchPolicy()
    decoding: DecodingSettings = DecodingSettings()
    rate_limit_retries: int = 1
    max_retry_wait_ms: int = 60_000

    def __post_init__(self) -> None:
        if (type(self.prompt) is not PromptTemplate or type(self.batch) is not BatchPolicy
                or type(self.decoding) is not DecodingSettings):
            raise LlmCoreError("invalid_llm_settings")
        if type(self.rate_limit_retries) is not int or not 0 <= self.rate_limit_retries <= 8:
            raise LlmCoreError("invalid_llm_settings")
        if type(self.max_retry_wait_ms) is not int or not 0 <= self.max_retry_wait_ms <= MAX_RETRY_WAIT_MS:
            raise LlmCoreError("invalid_llm_settings")

    def __repr__(self) -> str:
        return f"LlmSettings(prompt_revision={self.prompt.revision})"

    def semantics(self) -> LlmSemantics:
        parameters = (("llm_core_version", "integer", LLM_CORE_VERSION),
                      ("output_schema_revision", "integer", OUTPUT_SCHEMA_REVISION),
                      *self.batch.parameters(), *self.decoding.parameters())
        return LlmSemantics(PROMPT_STRATEGY, self.prompt.revision, f"llm-prompt-v{self.prompt.revision}",
                            f"{PROMPT_SNAPSHOT_KIND}:{self.prompt.digest}", tuple(sorted(parameters)))
