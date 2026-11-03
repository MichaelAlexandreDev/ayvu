from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction

from .cache import CacheKey, TranslationCache
from .domain import LanguagePair
from .glossary import Glossary, GlossaryUsage, apply_glossary_with_usage
from .translation_memory import TranslationMemory, TranslationMemoryMatch
from .translation_pipeline import LegacyTextPipelineFacade, LinguisticSource
from .translation_protection import TAG_TOKEN_PATTERN, protected_terms_are_intact as _protected_terms_are_intact
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
MAX_PROTECTED_RECOVERY_CALLS = 64
TAG_PLACEHOLDER_PREFIX = "__AYVU_TAG_"
TAG_PLACEHOLDER_SUFFIX = "__"
TAG_MARKUP_SENTINEL = "AYVU_TAG_MARKUP_SENTINEL"
FRAGMENT_ROOT_TAG = "__ayvu_fragment_root__"
TextProgressCallback = Callable[[str], None]








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


class _LegacyV1Cache:
    """The current cache behind the pipeline port, selected explicitly by this facade alone."""

    def __init__(self, cache: TranslationCache) -> None:
        self._cache = cache

    def get(self, text: str, language_pair: LanguagePair) -> str | None:
        return self._cache.get(CacheKey(text=text, language_pair=language_pair))

    def set(self, text: str, language_pair: LanguagePair, translated: str) -> None:
        self._cache.set(CacheKey(text=text, language_pair=language_pair), translated)


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
    outcome = LegacyTextPipelineFacade(
        translator,
        _LegacyV1Cache(cache),
        lambda translated: apply_glossary_with_usage(translated, glossary),
        memory=memory,
        max_recovery_calls=MAX_PROTECTED_RECOVERY_CALLS,
    ).run(
        text,
        LanguagePair(source=source, target=target),
        dry_run=dry_run,
        cache_only=cache_only,
        chunk_limit=chunk_limit,
    )
    return TextTranslationResult(
        text=outcome.text,
        from_cache=outcome.source is LinguisticSource.EXACT_CACHE,
        from_memory=outcome.source is LinguisticSource.APPLIED_MEMORY,
        missing=outcome.source is LinguisticSource.CACHE_ONLY_MISSING,
        glossary_usage=GlossaryUsage() if outcome.glossary_usage is None else outcome.glossary_usage,
        memory_suggestion=outcome.memory_suggestion,
    )












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
