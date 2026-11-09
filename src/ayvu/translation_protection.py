"""Keeps technical terms and placeholders out of a provider's hands, and checks they survived.

Pure text rules with no knowledge of any format, moved unchanged from the HTML module.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass

PROTECTED_PLACEHOLDER_PREFIX = "__AYVU_PROTECTED_"
PROTECTED_PLACEHOLDER_SUFFIX = "__"
PROTECTED_TOKEN_PATTERN = re.compile(r"__AYVU_PROTECTED_\d+__")
ORDERED_PLACEHOLDER_PATTERN = re.compile(r"__AYVU_(?:TAG|PROTECTED)_\d+__")
# Also recognize common provider rewrites in translations saved by older versions.
INTERNAL_MARKER_PATTERN = re.compile(
    r"AYVU[\W_]+(?:PROTECT\w*|PROTEG\w*|TAG)(?:[\W_]*\d+)?_*", re.I
)
PROTECTED_TERM_SCAN_STEP_MULTIPLIER = 4
MIN_PROTECTED_TERM_SCAN_STEPS = 256
TAG_TOKEN_PATTERN = re.compile(r"__AYVU_TAG_(\d+)__")


SPECIAL_TERM_PATTERNS = (
    PROTECTED_TOKEN_PATTERN,
    TAG_TOKEN_PATTERN,
    re.compile(r"`[^`\n]+`"),
    re.compile(r"\{\{[^{}\n]+\}\}"),
    re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}"),
    re.compile(r"%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[sd]"),
    re.compile(r"(?m)(?<!\S)(?:[$#>]\s*)[^\n]+"),
    re.compile(
        r"(?<![\w-])"
        r"(?:ayvu|uv|git|docker|pip|python3?|pytest|curl|wget|npm|node|cargo|poetry)"
        r"(?:\s+(?:[^\s`<>(),;!?]+))+",
    ),
    re.compile(r"https?://[^\s`<>()\"']+"),
    re.compile(r"(?<![\w./-])(?:~|\.{1,2})?/[^\s`<>()\"']+"),
    re.compile(r"(?<![\w./-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"),
    re.compile(r"(?<![\w./-])[A-Za-z]:\\[^\s`<>()\"']+"),
    re.compile(r"(?<![\w.-])v?\d+\.\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?\b"),
    re.compile(r"(?<!\w)--?[A-Za-z][A-Za-z0-9-]*(?:=[^\s`<>()]+)?"),
    re.compile(r"(?<!\w)\$[A-Z_][A-Z0-9_]*\b"),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\(\)"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*\b"),
    re.compile(r"\b[A-Za-z]+[A-Z][A-Za-z0-9]*\b"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"),
)


@dataclass(frozen=True)
class ProtectedSpan:
    start: int
    end: int


@dataclass(frozen=True)
class ProtectedText:
    text: str
    terms: tuple[tuple[str, str], ...] = ()

    def restore(self, translated: str) -> str:
        originals = dict(self.terms)
        return PROTECTED_TOKEN_PATTERN.sub(
            lambda match: originals.get(match.group(0), match.group(0)), translated
        )


@dataclass(frozen=True)
class TextParts:
    leading: str
    core: str
    trailing: str

    @classmethod
    def from_text(cls, text: str) -> "TextParts":
        leading = text[: len(text) - len(text.lstrip())]
        trailing = text[len(text.rstrip()) :]
        return cls(leading=leading, core=text.strip(), trailing=trailing)

    def restore(self, core: str) -> str:
        return self.leading + core + self.trailing


def protected_content_is_intact(original: str, translated: str) -> bool:
    extra_markers = Counter(INTERNAL_MARKER_PATTERN.findall(translated)) - Counter(
        INTERNAL_MARKER_PATTERN.findall(original)
    )
    if extra_markers:
        return False
    if ORDERED_PLACEHOLDER_PATTERN.findall(original) != ORDERED_PLACEHOLDER_PATTERN.findall(
        translated
    ):
        return False
    terms = Counter(original[span.start:span.end] for span in special_term_spans(original))
    return protected_terms_are_intact(translated, terms)


def protected_terms_are_intact(text: str, terms: Counter[str]) -> bool:
    lexical_terms = {
        term: count
        for term, count in terms.items()
        if not TAG_TOKEN_PATTERN.fullmatch(term)
        and not PROTECTED_TOKEN_PATTERN.fullmatch(term)
    }
    if not lexical_terms:
        return True

    # Tag placeholders are boundaries around visible text, even though their
    # underscores would otherwise make an adjacent identifier look concatenated.
    searchable = TAG_TOKEN_PATTERN.sub(lambda match: " " * len(match.group(0)), text)
    searchable = PROTECTED_TOKEN_PATTERN.sub(
        lambda match: " " * len(match.group(0)), searchable
    )

    transitions: list[dict[str, int]] = [{}]
    outputs: list[list[str]] = [[]]
    failures = [0]
    output_links: list[int | None] = [None]
    for term in lexical_terms:
        state = 0
        for character in term:
            next_state = transitions[state].get(character)
            if next_state is None:
                next_state = len(transitions)
                transitions[state][character] = next_state
                transitions.append({})
                outputs.append([])
                failures.append(0)
                output_links.append(None)
            state = next_state
        outputs[state].append(term)

    pending = deque(transitions[0].values())
    while pending:
        state = pending.popleft()
        for character, next_state in transitions[state].items():
            fallback = failures[state]
            while fallback and character not in transitions[fallback]:
                fallback = failures[fallback]
            failures[next_state] = transitions[fallback].get(character, 0)
            failure_state = failures[next_state]
            output_links[next_state] = (
                failure_state if outputs[failure_state] else output_links[failure_state]
            )
            pending.append(next_state)

    actual_terms: Counter[str] = Counter()
    scan_budget = max(
        MIN_PROTECTED_TERM_SCAN_STEPS,
        len(searchable) * PROTECTED_TERM_SCAN_STEP_MULTIPLIER,
    )
    scan_steps = 0
    state = 0
    for end, character in enumerate(searchable):
        while state and character not in transitions[state]:
            state = failures[state]
        state = transitions[state].get(character, 0)
        after = searchable[end + 1] if end + 1 < len(searchable) else ""
        if _is_word_character(after):
            continue
        output_state = state if outputs[state] else output_links[state]
        while output_state is not None:
            for term in outputs[output_state]:
                scan_steps += 1
                if scan_steps > scan_budget:
                    return False
                start = end - len(term) + 1
                before = searchable[start - 1] if start else ""
                if _is_word_character(before):
                    continue
                actual_terms[term] += 1
                if actual_terms[term] > lexical_terms[term]:
                    return False
            output_state = output_links[output_state]
    return actual_terms == lexical_terms


def _is_word_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def protect_special_terms(text: str) -> ProtectedText:
    spans = special_term_spans(text)
    if not spans:
        return ProtectedText(text=text)

    protected_parts: list[str] = []
    terms: list[tuple[str, str]] = []
    cursor = 0
    for span in spans:
        placeholder = _protected_placeholder(len(terms))
        protected_parts.append(text[cursor : span.start])
        protected_parts.append(placeholder)
        terms.append((placeholder, text[span.start : span.end]))
        cursor = span.end

    protected_parts.append(text[cursor:])
    return ProtectedText(text="".join(protected_parts), terms=tuple(terms))


def special_term_spans(text: str) -> list[ProtectedSpan]:
    spans: list[ProtectedSpan] = []
    # Token underscores must not hide word boundaries next to inline markup.
    # Keep offsets identical so detected terms still refer to the original text.
    plain = TAG_TOKEN_PATTERN.sub(lambda match: " " * len(match.group(0)), text)
    plain = PROTECTED_TOKEN_PATTERN.sub(lambda match: " " * len(match.group(0)), plain)
    for pattern in SPECIAL_TERM_PATTERNS:
        searchable = text if pattern in (TAG_TOKEN_PATTERN, PROTECTED_TOKEN_PATTERN) else plain
        for match in pattern.finditer(searchable):
            span = _clean_match_span(text, match.start(), match.end())
            if span is None or _overlaps_any(span, spans):
                continue
            spans.append(span)
    return sorted(spans, key=lambda span: span.start)


def _clean_match_span(text: str, start: int, end: int) -> ProtectedSpan | None:
    while start < end and text[start].isspace():
        start += 1
    while start < end and text[end - 1] in ".,;:!?":
        end -= 1
    while (
        start < end
        and text[end - 1] in ")]}"
        and _has_unmatched_closer(text[start:end], text[end - 1])
    ):
        end -= 1
    if start >= end:
        return None
    return ProtectedSpan(start=start, end=end)


def _has_unmatched_closer(value: str, closer: str) -> bool:
    opener = {")": "(", "]": "[", "}": "{"}[closer]
    return value.count(closer) > value.count(opener)


def _overlaps_any(candidate: ProtectedSpan, spans: list[ProtectedSpan]) -> bool:
    return any(candidate.start < span.end and candidate.end > span.start for span in spans)


def _protected_placeholder(index: int) -> str:
    return f"{PROTECTED_PLACEHOLDER_PREFIX}{index}{PROTECTED_PLACEHOLDER_SUFFIX}"
