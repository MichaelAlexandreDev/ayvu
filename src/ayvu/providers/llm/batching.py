"""Group the units of one document into requests, in order, within a policy's bounds."""
from __future__ import annotations

import json
import math

from .model import BatchPolicy
from .prompt import PromptSegment


def estimate_tokens(text: str, policy: BatchPolicy) -> int:
    return math.ceil(len(text) / policy.chars_per_token)


def segment_tokens(segment: PromptSegment, policy: BatchPolicy) -> int:
    """The cost of a segment as it is rendered, including its context and JSON quoting."""
    return estimate_tokens(json.dumps(segment.payload(), ensure_ascii=False, separators=(",", ":")) + ",", policy)


def plan_batches(segments: tuple[PromptSegment, ...], policy: BatchPolicy,
                 overhead_tokens: int = 0) -> tuple[tuple[int, ...], ...]:
    """Indices of ``segments``, grouped in order. A batch closes before it would pass a bound.

    A segment that alone exceeds the budget still forms a batch of its own: units are never split
    here, since the pipeline's chunk limit already bounds them.
    """
    batches: list[tuple[int, ...]] = []
    current: list[int] = []
    used = overhead_tokens
    for index, segment in enumerate(segments):
        cost = segment_tokens(segment, policy)
        if current and (len(current) >= policy.max_units or used + cost > policy.token_budget):
            batches.append(tuple(current))
            current, used = [], overhead_tokens
        current.append(index)
        used += cost
    if current:
        batches.append(tuple(current))
    return tuple(batches)
