from __future__ import annotations

import copy
import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction

from .cache import CacheKey, TranslationCache
from .chunking import split_text
from .domain import LanguagePair
from .glossary import Glossary, GlossaryUsage, apply_glossary_with_usage
from .translation_memory import TranslationMemory, TranslationMemoryMatch
from .translator import Translator


IGNORED_TAGS = {"script", "style", "code", "pre", "kbd", "samp", "svg", "math"}
# Ignored tags that may appear inline inside a block. Their whole markup is kept
# untranslated as an opaque token instead of acting as a block boundary.
IGNORED_INLINE_TAGS = {"code", "kbd", "samp"}
# Inline tags whose text is translated together with the surrounding block. The
# tags themselves are replaced by neutral placeholders and restored afterwards.
INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "br", "cite", "data", "del", "dfn", "em",
    "i", "ins", "mark", "q", "rp", "rt", "ruby", "s", "small", "span", "strike",
    "strong", "sub", "sup", "time", "u", "var", "wbr",
}
PROTECTED_PLACEHOLDER_PREFIX = "__AYVU_PROTECTED_"
PROTECTED_PLACEHOLDER_SUFFIX = "__"
PROTECTED_TOKEN_PATTERN = re.compile(r"__AYVU_PROTECTED_\d+__")
ORDERED_PLACEHOLDER_PATTERN = re.compile(r"__AYVU_(?:TAG|PROTECTED)_\d+__")
# Also recognize common provider rewrites in translations saved by older versions.
INTERNAL_MARKER_PATTERN = re.compile(
    r"AYVU[\W_]+(?:PROTECT\w*|PROTEG\w*|TAG)(?:[\W_]*\d+)?_*", re.I
)
MAX_PROTECTED_RECOVERY_CALLS = 64
PROTECTED_TERM_SCAN_STEP_MULTIPLIER = 4
MIN_PROTECTED_TERM_SCAN_STEPS = 256
TAG_PLACEHOLDER_PREFIX = "__AYVU_TAG_"
TAG_PLACEHOLDER_SUFFIX = "__"
TAG_TOKEN_PATTERN = re.compile(r"__AYVU_TAG_(\d+)__")
TAG_MARKUP_SENTINEL = "AYVU_TAG_MARKUP_SENTINEL"
FRAGMENT_ROOT_TAG = "__ayvu_fragment_root__"
TextProgressCallback = Callable[[str], None]


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


@dataclass
class HtmlTranslationStats:
    translated: int = 0
    from_cache: int = 0
    from_memory: int = 0
    memory_suggestions: int = 0
    skipped: int = 0
    alt_translated: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)
    missing_texts: list[str] = field(default_factory=list)
    memory_suggestion_texts: list[str] = field(default_factory=list)
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)


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


@dataclass(frozen=True)
class TextTranslationResult:
    text: str
    from_cache: bool = False
    from_memory: bool = False
    missing: bool = False
    glossary_usage: GlossaryUsage = field(default_factory=GlossaryUsage)
    memory_suggestion: TranslationMemoryMatch | None = None


@dataclass(frozen=True)
class HtmlTranslatedSegment:
    original: str
    translated: str
    kind: str
    from_cache: bool = False


SegmentReviewCallback = Callable[[HtmlTranslatedSegment], None]

# Resolves a reviewed translation for one segment. Receives the per-document
# segment index (1-based, in emission order), the segment kind ("text"/"alt")
# and the visible original text re-derived from the source EPUB. Returns the
# replacement text to apply, or ``None`` to leave the segment untouched.
ApplyResolver = Callable[[int, str, str], "str | None"]


@dataclass
class ApplyHtmlStats:
    applied: int = 0
    skipped: int = 0


def extract_visible_text(html: str | bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml-xml")
    return [str(text_node) for text_node in _visible_text_nodes(soup) if str(text_node).strip()]


def translate_html(
    html: str | bytes,
    translator: Translator,
    cache: TranslationCache,
    source: str,
    target: str,
    glossary: Glossary | None = None,
    dry_run: bool = False,
    cache_only: bool = False,
    fail_fast: bool = False,
    chunk_limit: int = 3000,
    memory: TranslationMemory | None = None,
    on_error: Callable[[Exception], None] | None = None,
    on_text_processed: TextProgressCallback | None = None,
    on_segment_translated: SegmentReviewCallback | None = None,
    translate_alt_text: bool = False,
) -> tuple[bytes, HtmlTranslationStats]:
    soup = BeautifulSoup(html, "lxml-xml")
    stats = HtmlTranslationStats()

    for run in _collect_translation_runs(soup):
        template, tag_markup = _build_block_template(run)
        if not _has_translatable_text(template):
            stats.skipped += 1
            continue

        try:
            result = translate_text(
                template,
                translator=translator,
                cache=cache,
                source=source,
                target=target,
                glossary=glossary,
                dry_run=dry_run,
                cache_only=cache_only,
                chunk_limit=chunk_limit,
                memory=memory,
            )
            if not dry_run and not result.missing:
                fragment = _expand_tag_tokens(escape(result.text), tag_markup)
                _replace_run(run, _parse_fragment_nodes(fragment))
            if result.missing:
                stats.missing_texts.append(_review_text_from_template(template, tag_markup))
            _notify_segment_translated(
                on_segment_translated,
                original_template=template,
                translated_template=result.text,
                tag_markup=tag_markup,
                kind="text",
                from_cache=result.from_cache,
            )
            stats.glossary_usage.merge(result.glossary_usage)
            _record_success(stats, result, dry_run, on_text_processed)
        except Exception as exc:
            stats.errors.append(str(exc))
            _notify_text_processed(on_text_processed, "error")
            if on_error:
                on_error(exc)
            if fail_fast:
                raise

    if translate_alt_text:
        _translate_image_alt_text(
            soup,
            translator=translator,
            cache=cache,
            source=source,
            target=target,
            glossary=glossary,
            dry_run=dry_run,
            cache_only=cache_only,
            fail_fast=fail_fast,
            chunk_limit=chunk_limit,
            memory=memory,
            stats=stats,
            on_error=on_error,
            on_text_processed=on_text_processed,
            on_segment_translated=on_segment_translated,
        )

    return soup.encode(formatter="minimal"), stats


def translate_text(
    text: str,
    translator: Translator,
    cache: TranslationCache,
    source: str,
    target: str,
    glossary: Glossary | None = None,
    dry_run: bool = False,
    cache_only: bool = False,
    chunk_limit: int = 3000,
    memory: TranslationMemory | None = None,
) -> TextTranslationResult:
    parts = TextParts.from_text(text)
    if not parts.core:
        return TextTranslationResult(text=text)

    language_pair = LanguagePair(source=source, target=target)
    cache_key = CacheKey(text=parts.core, language_pair=language_pair)
    cached = cache.get(cache_key)
    if cached is not None and _protected_content_is_intact(parts.core, cached):
        application = apply_glossary_with_usage(cached, glossary)
        return TextTranslationResult(
            text=parts.restore(application.text),
            from_cache=True,
            glossary_usage=application.usage,
        )

    suggestion = _lookup_memory_suggestion(memory, parts.core, language_pair)
    if suggestion is not None and suggestion.applied:
        application = apply_glossary_with_usage(suggestion.translated, glossary)
        return TextTranslationResult(
            text=parts.restore(application.text),
            from_memory=True,
            glossary_usage=application.usage,
        )

    if cache_only:
        return TextTranslationResult(text=text, missing=True, memory_suggestion=suggestion)

    if dry_run:
        return TextTranslationResult(text=text, memory_suggestion=suggestion)

    protected = _protect_special_terms(parts.core)
    chunks = split_text(protected.text, limit=chunk_limit)
    expected_tokens = [token for token, _ in protected.terms]
    # Small chunk limits can split a token. Never send those broken tokens.
    chunk_tokens = [
        token for chunk in chunks for token in PROTECTED_TOKEN_PATTERN.findall(chunk)
    ]
    tokens_fit = chunk_tokens == expected_tokens
    translated = None
    if tokens_fit:
        response = "".join(translator.translate(chunk, source, target) for chunk in chunks)
        if response.strip() and PROTECTED_TOKEN_PATTERN.findall(response) == expected_tokens:
            restored = protected.restore(response)
            if _protected_content_is_intact(parts.core, restored):
                translated = restored
    if translated is None:
        translated = _translate_around_protected_terms(
            parts.core, translator, source, target, chunk_limit
        )
    if not _protected_content_is_intact(parts.core, translated):
        raise ValueError("O tradutor devolveu marcadores internos ou perdeu termos protegidos.")
    cache.set(cache_key, translated)
    application = apply_glossary_with_usage(translated, glossary)
    return TextTranslationResult(
        text=parts.restore(application.text),
        glossary_usage=application.usage,
        memory_suggestion=suggestion,
    )


def _lookup_memory_suggestion(
    memory: TranslationMemory | None,
    core: str,
    language_pair: LanguagePair,
) -> TranslationMemoryMatch | None:
    """Find a fuzzy match for plain-text segments only.

    Segments carrying inline-tag placeholders are skipped, and matches whose
    stored text carries placeholders are rejected, so a reused translation can
    never inject another segment's markup. This keeps the memory conservative
    and limited to plain prose, where similar phrasing is most common.
    """
    if memory is None or TAG_TOKEN_PATTERN.search(core):
        return None
    match = memory.lookup(core, language_pair)
    if match is None:
        return None
    if TAG_TOKEN_PATTERN.search(match.original) or TAG_TOKEN_PATTERN.search(match.translated):
        return None
    if not _protected_content_is_intact(core, match.translated):
        return None
    return match


def _protected_content_is_intact(original: str, translated: str) -> bool:
    extra_markers = Counter(INTERNAL_MARKER_PATTERN.findall(translated)) - Counter(
        INTERNAL_MARKER_PATTERN.findall(original)
    )
    if extra_markers:
        return False
    if ORDERED_PLACEHOLDER_PATTERN.findall(original) != ORDERED_PLACEHOLDER_PATTERN.findall(
        translated
    ):
        return False
    terms = Counter(original[span.start:span.end] for span in _special_term_spans(original))
    return _protected_terms_are_intact(translated, terms)


def _protected_terms_are_intact(text: str, terms: Counter[str]) -> bool:
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


def _translate_around_protected_terms(
    text: str, translator: Translator, source: str, target: str, chunk_limit: int
) -> str:
    """Retry with plain fragments; protected content never reaches the provider.

    This bounded fallback sacrifices some sentence context to preserve technical
    terms and inline markup when the provider cannot preserve our tokens.
    """
    output: list[str] = []
    cursor = 0
    recovery_calls = 0
    for span in [*_special_term_spans(text), ProtectedSpan(len(text), len(text))]:
        parts = TextParts.from_text(text[cursor:span.start])
        if parts.core:
            fragments: list[str] = []
            offset = 0
            for chunk in split_text(parts.core, limit=chunk_limit):
                start = parts.core.index(chunk, offset)
                fragments.append(parts.core[offset:start])
                chunk_parts = TextParts.from_text(chunk)
                if not chunk_parts.core:
                    fragments.append(chunk)
                    offset = start + len(chunk)
                    continue
                if recovery_calls >= MAX_PROTECTED_RECOVERY_CALLS:
                    raise ValueError(
                        "A recuperação excedeu o limite de chamadas ao tradutor."
                    )
                recovery_calls += 1
                result = translator.translate(chunk_parts.core, source, target)
                if not result.strip():
                    raise ValueError("O tradutor devolveu texto vazio durante a recuperação.")
                fragments.append(chunk_parts.restore(result.strip()))
                offset = start + len(chunk)
            fragments.append(parts.core[offset:])
            translated = "".join(fragments)
            if INTERNAL_MARKER_PATTERN.search(translated):
                raise ValueError("O tradutor devolveu marcadores internos durante a recuperação.")
            output.append(parts.restore(translated))
        else:
            output.append(text[cursor:span.start])
        output.append(text[span.start:span.end])
        cursor = span.end
    return "".join(output)


def apply_reviewed_html(
    html: str | bytes,
    resolve: ApplyResolver,
) -> tuple[bytes, ApplyHtmlStats]:
    """Apply reviewed translations to an HTML/XHTML document.

    Walks the same translation runs as :func:`translate_html` so per-document
    segment indices line up with the review export. The translator is never
    called: each translatable run (and image ``alt`` text) is offered to
    ``resolve``; when it returns a string the run's visible text is replaced by
    that plain text. Inline tags inside a replaced run are not restored because
    the review file stores plain visible text only.
    """
    soup = BeautifulSoup(html, "lxml-xml")
    stats = ApplyHtmlStats()
    index = 0

    for run in _collect_translation_runs(soup):
        template, tag_markup = _build_block_template(run)
        if not _has_translatable_text(template):
            stats.skipped += 1
            continue
        index += 1
        original = _review_text_from_template(template, tag_markup)
        replacement = resolve(index, "text", original)
        if replacement is not None:
            _replace_run(run, _parse_fragment_nodes(escape(replacement)))
            stats.applied += 1

    for image in _image_elements(soup):
        alt = image.get("alt")
        if not isinstance(alt, str) or not alt.strip():
            continue
        index += 1
        original = _review_text_from_template(alt, [])
        replacement = resolve(index, "alt", original)
        if replacement is not None:
            image["alt"] = replacement
            stats.applied += 1

    return soup.encode(formatter="minimal"), stats


def _translate_image_alt_text(
    soup: BeautifulSoup,
    translator: Translator,
    cache: TranslationCache,
    source: str,
    target: str,
    glossary: Glossary | None,
    dry_run: bool,
    cache_only: bool,
    fail_fast: bool,
    chunk_limit: int,
    memory: TranslationMemory | None,
    stats: HtmlTranslationStats,
    on_error: Callable[[Exception], None] | None,
    on_text_processed: TextProgressCallback | None,
    on_segment_translated: SegmentReviewCallback | None,
) -> None:
    """Translate the ``alt`` text of ``img`` elements as plain text.

    Only the alternative description is translated; the image, its ``src`` and
    every other attribute are preserved. Reading text rendered inside the image
    (OCR) is intentionally out of scope.
    """
    for image in _image_elements(soup):
        alt = image.get("alt")
        if not isinstance(alt, str) or not alt.strip():
            continue
        try:
            result = translate_text(
                alt,
                translator=translator,
                cache=cache,
                source=source,
                target=target,
                glossary=glossary,
                dry_run=dry_run,
                cache_only=cache_only,
                chunk_limit=chunk_limit,
                memory=memory,
            )
            if not dry_run and not result.missing:
                image["alt"] = result.text
            if result.missing:
                stats.missing_texts.append(_review_text_from_template(alt, []))
            _notify_segment_translated(
                on_segment_translated,
                original_template=alt,
                translated_template=result.text,
                tag_markup=[],
                kind="alt",
                from_cache=result.from_cache,
            )
            stats.glossary_usage.merge(result.glossary_usage)
            if not result.missing:
                stats.alt_translated += 1
            _record_success(stats, result, dry_run, on_text_processed)
        except Exception as exc:
            stats.errors.append(str(exc))
            _notify_text_processed(on_text_processed, "error")
            if on_error:
                on_error(exc)
            if fail_fast:
                raise


def _image_elements(soup: BeautifulSoup) -> list:
    return [node for node in soup.find_all(True) if _tag_name(node) == "img"]


def _visible_text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    return [text_node for text_node in soup.find_all(string=True) if _is_visible_text_node(text_node)]


def _collect_translation_runs(element) -> list[list]:
    """Group consecutive inline siblings into translation runs.

    Block-level and ignored-block children act as boundaries; we recurse into
    them so their own inline content becomes separate runs. This keeps loose
    text mixed with block elements from being lost.
    """
    runs: list[list] = []
    current: list = []
    for child in list(element.children):
        if _is_run_member(child):
            current.append(child)
            continue
        if current:
            runs.append(current)
            current = []
        if _should_recurse_into(child):
            runs.extend(_collect_translation_runs(child))
    if current:
        runs.append(current)
    return runs


def _is_run_member(node) -> bool:
    if _is_special_string(node):
        return False
    if isinstance(node, NavigableString):
        return True
    name = _tag_name(node)
    if name in IGNORED_INLINE_TAGS:
        return True
    if name in INLINE_TAGS:
        return not _contains_block_descendant(node)
    return False


def _should_recurse_into(node) -> bool:
    name = _tag_name(node)
    return bool(name) and name not in IGNORED_TAGS


def _contains_block_descendant(node) -> bool:
    return any(
        _tag_name(descendant) not in INLINE_TAGS
        and _tag_name(descendant) not in IGNORED_INLINE_TAGS
        for descendant in node.find_all(True)
    )


def _build_block_template(run: list) -> tuple[str, list[str]]:
    parts: list[str] = []
    tag_markup: list[str] = []
    for node in run:
        _emit_template_node(node, parts, tag_markup)
    return "".join(parts), tag_markup


def _emit_template_node(node, parts: list[str], tag_markup: list[str]) -> None:
    if _is_special_string(node):
        parts.append(_register_tag_markup(tag_markup, str(node)))
        return
    if isinstance(node, NavigableString):
        parts.append(str(node))
        return
    if _tag_name(node) in IGNORED_INLINE_TAGS or _is_void_element(node):
        parts.append(_register_tag_markup(tag_markup, str(node)))
        return

    open_markup, close_markup = _split_tag_markup(node)
    parts.append(_register_tag_markup(tag_markup, open_markup))
    for child in list(node.children):
        _emit_template_node(child, parts, tag_markup)
    parts.append(_register_tag_markup(tag_markup, close_markup))


def _register_tag_markup(tag_markup: list[str], markup: str) -> str:
    placeholder = f"{TAG_PLACEHOLDER_PREFIX}{len(tag_markup)}{TAG_PLACEHOLDER_SUFFIX}"
    tag_markup.append(markup)
    return placeholder


def _split_tag_markup(node) -> tuple[str, str]:
    clone = copy.copy(node)
    clone.clear()
    clone.append(NavigableString(TAG_MARKUP_SENTINEL))
    open_markup, _, close_markup = str(clone).partition(TAG_MARKUP_SENTINEL)
    return open_markup, close_markup


def _has_translatable_text(template: str) -> bool:
    return bool(TAG_TOKEN_PATTERN.sub("", template).strip())


def _expand_tag_tokens(text: str, tag_markup: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 0 <= index < len(tag_markup):
            return tag_markup[index]
        return match.group(0)

    return TAG_TOKEN_PATTERN.sub(replace, text)


def _parse_fragment_nodes(fragment: str) -> list:
    soup = BeautifulSoup(f"<{FRAGMENT_ROOT_TAG}>{fragment}</{FRAGMENT_ROOT_TAG}>", "lxml-xml")
    root = soup.find(FRAGMENT_ROOT_TAG)
    if root is None:
        return [NavigableString(fragment)]
    return [child.extract() for child in list(root.children)]


def _replace_run(run: list, new_nodes: list) -> None:
    if not new_nodes:
        return
    anchor = run[0]
    for node in new_nodes:
        anchor.insert_before(node)
    for node in run:
        node.extract()


def _is_special_string(node) -> bool:
    return isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction))


def _is_void_element(node) -> bool:
    return not node.contents


def _tag_name(node) -> str:
    name = getattr(node, "name", None)
    return name.lower() if isinstance(name, str) else ""


def _is_visible_text_node(text_node: NavigableString) -> bool:
    if isinstance(text_node, (Comment, Declaration, Doctype, ProcessingInstruction)):
        return False

    parent = text_node.parent
    while parent is not None and getattr(parent, "name", None):
        if str(parent.name).lower() in IGNORED_TAGS:
            return False
        parent = parent.parent
    return True


def _protect_special_terms(text: str) -> ProtectedText:
    spans = _special_term_spans(text)
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


def _special_term_spans(text: str) -> list[ProtectedSpan]:
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


def _notify_text_processed(callback: TextProgressCallback | None, status: str) -> None:
    if callback:
        callback(status)


def _notify_segment_translated(
    callback: SegmentReviewCallback | None,
    original_template: str,
    translated_template: str,
    tag_markup: list[str],
    kind: str,
    from_cache: bool,
) -> None:
    if callback is None:
        return

    callback(
        HtmlTranslatedSegment(
            original=_review_text_from_template(original_template, tag_markup),
            translated=_review_text_from_template(translated_template, tag_markup),
            kind=kind,
            from_cache=from_cache,
        )
    )


def _review_text_from_template(template: str, tag_markup: list[str]) -> str:
    fragment = _expand_tag_tokens(escape(template), tag_markup)
    soup = BeautifulSoup(f"<{FRAGMENT_ROOT_TAG}>{fragment}</{FRAGMENT_ROOT_TAG}>", "lxml-xml")
    root = soup.find(FRAGMENT_ROOT_TAG)
    if root is None:
        return TAG_TOKEN_PATTERN.sub("", template).strip()
    return "".join(_review_visible_text_parts(root)).strip()


def _review_visible_text_parts(node) -> list[str]:
    parts: list[str] = []
    for child in list(getattr(node, "children", [])):
        if _is_special_string(child):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue

        name = _tag_name(child)
        if name in {"script", "style", "svg", "math"}:
            continue
        if name == "br":
            parts.append("\n")
            continue
        parts.extend(_review_visible_text_parts(child))
    return parts


def _record_success(
    stats: HtmlTranslationStats,
    result: TextTranslationResult,
    dry_run: bool,
    on_text_processed: TextProgressCallback | None,
) -> None:
    if result.memory_suggestion is not None:
        stats.memory_suggestions += 1
        stats.memory_suggestion_texts.append(result.memory_suggestion.original)

    if result.from_memory:
        stats.from_memory += 1
        _notify_text_processed(on_text_processed, "memory")
        return

    if result.missing:
        stats.missing += 1
        _notify_text_processed(on_text_processed, "missing")
        return

    if result.from_cache:
        stats.from_cache += 1
        _notify_text_processed(on_text_processed, "cache")
        return

    stats.translated += 1
    if dry_run:
        _notify_text_processed(on_text_processed, "dry_run")
        return
    _notify_text_processed(on_text_processed, "translated")
