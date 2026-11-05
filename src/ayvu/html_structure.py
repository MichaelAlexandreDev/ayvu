"""Where reader-visible text is in an HTML or XHTML document, and how to put a translation back.

This module knows the document and nothing else: no provider, cache, memory, glossary or
chunking. Durable extraction gives ``ContentDocument`` units with opaque, versioned locators.
Reconstruction restores markup by copying original nodes, and translated text only ever
becomes text, so it cannot create an element, an attribute or a script.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction

from .content import (
    UNKNOWN_LOCALE, AdapterIdentity, ContentDocument, ContentError, Locator, ProtectedPart, SemanticKind, SourceLocale,
    TranslationUnit, UnitContent, build_document,
)
from .format_adapter import UnitTranslation
from .translation_protection import INTERNAL_MARKER_PATTERN, TAG_TOKEN_PATTERN

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
TAG_PLACEHOLDER_PREFIX = "__AYVU_TAG_"
TAG_PLACEHOLDER_SUFFIX = "__"
TAG_MARKUP_SENTINEL = "AYVU_TAG_MARKUP_SENTINEL"
FRAGMENT_ROOT_TAG = "__ayvu_fragment_root__"
_SLOT_TAG = "__ayvu_slot__"

# Bump when the segmentation or the meaning of a token changes: old documents are then refused, not remapped.
HTML_SEGMENTER_VERSION = 1
HTML_LOCATOR_SCHEMA = "html_position"
HTML_LOCATOR_VERSION = 1
HTML_ADAPTER = AdapterIdentity("html_structure", 1, HTML_SEGMENTER_VERSION)

_PARSER = "lxml-xml"
_RUN_PREFIX = "r:"
_ALT_PREFIX = "a:"
_XML_INVALID = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


class HtmlStructureError(ValueError):
    """Its message is always a bounded code, never document text."""


def extract_visible_text(html: str | bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml-xml")
    return [str(text_node) for text_node in visible_text_nodes(soup) if str(text_node).strip()]


def extract_body_visible_text(html: str | bytes) -> list[str]:
    """Returns reader-visible body text, matching EPUB document projections that omit the head."""
    soup = BeautifulSoup(html, "lxml-xml")
    body = soup.find("body")
    if body is None:
        return []
    return [str(text_node) for text_node in visible_text_nodes(body) if str(text_node).strip()]


def image_elements(soup: BeautifulSoup) -> list:
    return [node for node in soup.find_all(True) if tag_name(node) == "img"]


def visible_text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    return [text_node for text_node in soup.find_all(string=True) if is_visible_text_node(text_node)]


def collect_translation_runs(element) -> list[list]:
    """Group consecutive inline siblings into translation runs.

    Block-level and ignored-block children act as boundaries; we recurse into
    them so their own inline content becomes separate runs. This keeps loose
    text mixed with block elements from being lost.
    """
    runs: list[list] = []
    current: list = []
    for child in list(element.children):
        if is_run_member(child):
            current.append(child)
            continue
        if current:
            runs.append(current)
            current = []
        if should_recurse_into(child):
            runs.extend(collect_translation_runs(child))
    if current:
        runs.append(current)
    return runs


def is_run_member(node) -> bool:
    if is_special_string(node):
        return False
    if isinstance(node, NavigableString):
        return True
    name = tag_name(node)
    if name in IGNORED_INLINE_TAGS:
        return True
    if name in INLINE_TAGS:
        return not contains_block_descendant(node)
    return False


def should_recurse_into(node) -> bool:
    name = tag_name(node)
    return bool(name) and name not in IGNORED_TAGS


def contains_block_descendant(node) -> bool:
    return any(
        tag_name(descendant) not in INLINE_TAGS
        and tag_name(descendant) not in IGNORED_INLINE_TAGS
        for descendant in node.find_all(True)
    )


def split_tag_markup(node) -> tuple[str, str]:
    clone = copy.copy(node)
    clone.clear()
    clone.append(NavigableString(TAG_MARKUP_SENTINEL))
    open_markup, _, close_markup = str(clone).partition(TAG_MARKUP_SENTINEL)
    return open_markup, close_markup


def has_translatable_text(template: str) -> bool:
    return bool(TAG_TOKEN_PATTERN.sub("", template).strip())


def expand_tag_tokens(text: str, tag_markup: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 0 <= index < len(tag_markup):
            return tag_markup[index]
        return match.group(0)

    return TAG_TOKEN_PATTERN.sub(replace, text)


def parse_fragment_nodes(fragment: str) -> list:
    soup = BeautifulSoup(f"<{FRAGMENT_ROOT_TAG}>{fragment}</{FRAGMENT_ROOT_TAG}>", "lxml-xml")
    root = soup.find(FRAGMENT_ROOT_TAG)
    if root is None:
        return [NavigableString(fragment)]
    return [child.extract() for child in list(root.children)]


def replace_run(run: list, new_nodes: list) -> None:
    if not new_nodes:
        return
    anchor = run[0]
    for node in new_nodes:
        anchor.insert_before(node)
    for node in run:
        node.extract()


def is_special_string(node) -> bool:
    return isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction))


def is_void_element(node) -> bool:
    return not node.contents


def tag_name(node) -> str:
    name = getattr(node, "name", None)
    return name.lower() if isinstance(name, str) else ""


def is_visible_text_node(text_node: NavigableString) -> bool:
    if isinstance(text_node, (Comment, Declaration, Doctype, ProcessingInstruction)):
        return False

    parent = text_node.parent
    while parent is not None and getattr(parent, "name", None):
        if str(parent.name).lower() in IGNORED_TAGS:
            return False
        parent = parent.parent
    return True


def review_text_from_template(template: str, tag_markup: list[str]) -> str:
    fragment = expand_tag_tokens(escape(template), tag_markup)
    soup = BeautifulSoup(f"<{FRAGMENT_ROOT_TAG}>{fragment}</{FRAGMENT_ROOT_TAG}>", "lxml-xml")
    root = soup.find(FRAGMENT_ROOT_TAG)
    if root is None:
        return TAG_TOKEN_PATTERN.sub("", template).strip()
    return "".join(review_visible_text_parts(root)).strip()


def review_visible_text_parts(node) -> list[str]:
    parts: list[str] = []
    for child in list(getattr(node, "children", [])):
        if is_special_string(child):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue

        name = tag_name(child)
        if name in {"script", "style", "svg", "math"}:
            continue
        if name == "br":
            parts.append("\n")
            continue
        parts.extend(review_visible_text_parts(child))
    return parts


class _PartKind(Enum):
    OPEN = "open"
    CLOSE = "close"
    LEAF = "leaf"
    LITERAL = "literal"


@dataclass(frozen=True, eq=False)
class _Part:
    """One thing a token stands for. ``source`` is how it reads in the unit's text."""

    kind: _PartKind
    source: str
    node: object = None
    partner: int | None = None


@dataclass(frozen=True, eq=False)
class _Segment:
    """One unit's worth of a parsed document. It holds live nodes, so it never leaves this module."""

    is_alt: bool
    nodes: tuple
    template: str
    parts: tuple[_Part, ...]
    location: str

    def content(self, order: tuple[int, ...], *, locator: Locator | None = None, kind: SemanticKind | None = None) -> UnitContent:
        pieces: list[str] = []
        spans: list[ProtectedPart] = []
        offset = 0
        cursor = 0
        for match in TAG_TOKEN_PATTERN.finditer(self.template):
            plain = self.template[cursor:match.start()]
            part = self.parts[int(match.group(1))]
            pieces += [plain, part.source]
            start = offset + len(plain)
            offset = start + len(part.source)
            spans.append(ProtectedPart(start, offset, "placeholder" if part.kind is _PartKind.LITERAL else "markup"))
            cursor = match.end()
        pieces.append(self.template[cursor:])
        kind = kind or SemanticKind("alt_text" if self.is_alt else "paragraph")
        locator = locator or Locator(HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION, self.location)
        return UnitContent(locator, "".join(pieces), kind, order, protected_parts=tuple(spans))


def _parse(html: str | bytes) -> BeautifulSoup:
    if not isinstance(html, (str, bytes)):
        raise HtmlStructureError("invalid_html")
    return BeautifulSoup(html, _PARSER)


def _token(index: int) -> str:
    return f"{TAG_PLACEHOLDER_PREFIX}{index}{TAG_PLACEHOLDER_SUFFIX}"


class _TemplateBuilder:
    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.parts: list[_Part] = []

    def _register(self, part: _Part) -> int:
        self.pieces.append(_token(len(self.parts)))
        self.parts.append(part)
        return len(self.parts) - 1

    def text(self, value: str) -> None:
        # Book text that looks like a token is protected like markup, so it comes back unchanged.
        cursor = 0
        for match in TAG_TOKEN_PATTERN.finditer(value):
            self.pieces.append(value[cursor:match.start()])
            self._register(_Part(_PartKind.LITERAL, match.group(0)))
            cursor = match.end()
        self.pieces.append(value[cursor:])

    def node(self, node) -> None:
        if is_special_string(node):
            self._register(_Part(_PartKind.LEAF, node.output_ready(formatter="minimal"), node))
        elif isinstance(node, NavigableString):
            self.text(str(node))
        elif tag_name(node) in IGNORED_INLINE_TAGS or is_void_element(node):
            self._register(_Part(_PartKind.LEAF, str(node), node))
        else:
            open_markup, close_markup = split_tag_markup(node)
            opened = self._register(_Part(_PartKind.OPEN, open_markup, node))
            for child in list(node.children):
                self.node(child)
            self._register(_Part(_PartKind.CLOSE, close_markup, node, opened))


def _positions(soup: BeautifulSoup) -> dict[int, str]:
    """Each node's place in the parsed tree, by identity: equal siblings must not be confused."""
    positions: dict[int, str] = {}
    pending = [(soup, "")]
    while pending:
        parent, prefix = pending.pop()
        for index, child in enumerate(getattr(parent, "contents", ())):
            position = f"{prefix}.{index}" if prefix else str(index)
            positions[id(child)] = position
            pending.append((child, position))
    return positions


def _segments(soup: BeautifulSoup, *, include_alt_text: bool) -> list[_Segment]:
    positions = _positions(soup)
    segments: list[_Segment] = []
    for run in collect_translation_runs(soup):
        builder = _TemplateBuilder()
        for node in run:
            builder.node(node)
        template = "".join(builder.pieces)
        if has_translatable_text(template):
            segments.append(_Segment(False, tuple(run), template, tuple(builder.parts),
                                     f"{_RUN_PREFIX}{positions[id(run[0])]}+{len(run)}"))
    if include_alt_text:
        for image in image_elements(soup):
            alt = image.get("alt")
            if isinstance(alt, str) and alt.strip():
                builder = _TemplateBuilder()
                builder.text(alt)
                segments.append(_Segment(True, (image,), "".join(builder.pieces), tuple(builder.parts),
                                         f"{_ALT_PREFIX}{positions[id(image)]}"))
    return segments


def _document(segments: list[_Segment], project_id: str, logical_key: str, adapter: AdapterIdentity, source_locale: SourceLocale,
              source_fingerprint: str | None) -> ContentDocument:
    if type(adapter) is not AdapterIdentity or adapter.segmenter_version != HTML_SEGMENTER_VERSION:
        raise HtmlStructureError("incompatible_segmenter")
    try:
        # Segments already hold text first and alt text after it, as reviews and the current flow expect.
        units = [segment.content((index,)) for index, segment in enumerate(segments)]
        return build_document(project_id, logical_key, adapter, source_locale, units, source_fingerprint=source_fingerprint)
    except ContentError as error:  # A unit that breaks a limit of the content model, such as its size.
        raise HtmlStructureError(str(error)) from None


def extract_html_document(html: str | bytes, project_id: str, logical_key: str, *, adapter: AdapterIdentity = HTML_ADAPTER,
                          source_locale: SourceLocale = UNKNOWN_LOCALE, include_alt_text: bool = False,
                          source_fingerprint: str | None = None) -> ContentDocument:
    """Reader-visible text as durable units. The input is only read, and parsing changes nothing in it."""
    segments = _segments(_parse(html), include_alt_text=include_alt_text)
    return _document(segments, project_id, logical_key, adapter, source_locale, source_fingerprint)


class TransientHtmlRun:
    """One run of the current translation flow. It has no identity, and nothing durable can refer to it."""

    __slots__ = ("nodes", "template", "markup", "_parts")

    def __init__(self, nodes: list, template: str, parts: tuple[_Part, ...]) -> None:
        self.nodes = nodes
        self.template = template
        self.markup = [part.source for part in parts]
        self._parts = parts

    def __repr__(self) -> str:
        return "TransientHtmlRun()"

    def __reduce_ex__(self, protocol: object) -> None:
        raise TypeError("a transient HTML run cannot be serialized")


class TransientHtmlDocument:
    """The current flow's view of a document, for a signature that has no project or revision.

    It shares the traversal and the restoration of markup with durable reconstruction, but it
    holds no locator or unit ID, and it cannot be serialized, so it can never pass for either.
    """

    def __init__(self, html: str | bytes) -> None:
        self._soup = BeautifulSoup(html, _PARSER)
        self.runs: list[TransientHtmlRun] = []
        for run in collect_translation_runs(self._soup):
            builder = _TemplateBuilder()
            for node in run:
                builder.node(node)
            self.runs.append(TransientHtmlRun(run, "".join(builder.pieces), tuple(builder.parts)))

    def __repr__(self) -> str:
        return "TransientHtmlDocument()"

    def __reduce_ex__(self, protocol: object) -> None:
        raise TypeError("a transient HTML document cannot be serialized")

    def images(self) -> list:
        return image_elements(self._soup)

    def replace_with_plain_text(self, run: TransientHtmlRun, text: str) -> None:
        replace_run(run.nodes, parse_fragment_nodes(escape(text)))

    def restore(self, run: TransientHtmlRun, text: str) -> None:
        """Parses the escaped text with an empty marker per token, then swaps each marker for original nodes.

        This keeps the parser's reading of the text as it has always been, and markup never goes
        through a string, which is what used to lose namespace prefixes and comments.
        """
        def slot(match: re.Match[str]) -> str:
            index = int(match.group(1))
            if not 0 <= index < len(run._parts):
                return match.group(0)
            # Text that only looks like a token goes back as text, so its node is read as one piece.
            part = run._parts[index]
            return part.source if part.kind is _PartKind.LITERAL else f'<{_SLOT_TAG} n="{index}"/>'

        fragment = TAG_TOKEN_PATTERN.sub(slot, escape(text))
        top: list = []
        stack: list = []

        def add(node) -> None:
            if stack:
                stack[-1].append(node)
            else:
                top.append(node)

        for node in parse_fragment_nodes(fragment):
            if tag_name(node) != _SLOT_TAG:
                if isinstance(node, NavigableString) and _SLOT_TAG in node:
                    # The parser could not read the fragment at all, and handed back the raw string.
                    node = NavigableString(expand_tag_tokens(escape(text), run.markup))
                add(node)
                continue
            part = run._parts[int(node["n"])]
            if part.kind is _PartKind.OPEN:
                clone = _clone_open(part.node)
                add(clone)
                stack.append(clone)
            elif part.kind is _PartKind.CLOSE:
                if stack:
                    stack.pop()
            else:
                add(copy.copy(part.node))
        replace_run(run.nodes, top)

    def encode(self) -> bytes:
        return self._soup.encode(formatter="minimal")


def plain_text_content(text: str, locator: Locator, kind: SemanticKind, order: tuple[int, ...]) -> UnitContent:
    """A unit for text outside a document body. It follows the same token rules, so it fails the same way."""
    builder = _TemplateBuilder()
    builder.text(text)
    segment = _Segment(True, (), "".join(builder.pieces), tuple(builder.parts), locator.value)
    return segment.content(order, locator=locator, kind=kind)


def restore_plain_text(unit: TranslationUnit, text: str) -> str:
    """Checks tokens, and puts back text that only looked like one. The result is still only text."""
    builder = _TemplateBuilder()
    builder.text(unit.source_text)
    segment = _Segment(True, (), "".join(builder.pieces), tuple(builder.parts), unit.content.locator.value)
    return _alt_text(segment, _plan(segment, text))


def html_unit_template(unit: TranslationUnit) -> str:
    """The unit's text with each protected part as its token, which is what reconstruction expects back."""
    text = unit.source_text
    pieces: list[str] = []
    cursor = 0
    for index, part in enumerate(unit.content.protected_parts):
        pieces += [text[cursor:part.start], _token(index)]
        cursor = part.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def without_xml_invalid(text: str) -> str:
    return _XML_INVALID.sub("", text)


def normalize_translated_text(text: str) -> str:
    """What an XML parser would do to a provider's text, so that reconstruction never refuses it."""
    return without_xml_invalid(text).replace("\r\n", "\n").replace("\r", "\n")


def _plan(segment: _Segment, text: str) -> list:
    """Checks every token of one unit, and returns plain text and part positions in order. Touches no node."""
    plain_translation = TAG_TOKEN_PATTERN.sub(" ", text)
    plain_source = TAG_TOKEN_PATTERN.sub(" ", segment.template)
    if Counter(INTERNAL_MARKER_PATTERN.findall(plain_translation)) - Counter(INTERNAL_MARKER_PATTERN.findall(plain_source)):
        raise HtmlStructureError("malformed_token")
    if _XML_INVALID.search(TAG_TOKEN_PATTERN.sub("", text)):
        raise HtmlStructureError("invalid_text")
    plan: list = []
    seen: set[int] = set()
    open_stack: list[int] = []
    cursor = 0
    for match in TAG_TOKEN_PATTERN.finditer(text):
        if match.start() > cursor:
            plan.append(text[cursor:match.start()])
        cursor = match.end()
        digits = match.group(1)
        if len(digits) > 9 or str(int(digits)) != digits:
            raise HtmlStructureError("malformed_token")
        index = int(digits)
        if index >= len(segment.parts):
            raise HtmlStructureError("invented_token")
        if index in seen:
            raise HtmlStructureError("duplicate_token")
        seen.add(index)
        part = segment.parts[index]
        if part.kind is _PartKind.OPEN:
            open_stack.append(index)
        elif part.kind is _PartKind.CLOSE:
            if not open_stack or open_stack.pop() != part.partner:
                raise HtmlStructureError("invalid_token_order")
        plan.append(index)
    if cursor < len(text):
        plan.append(text[cursor:])
    if len(seen) != len(segment.parts):
        raise HtmlStructureError("missing_token")
    return plan


def _clone_open(node):
    clone = copy.copy(node)
    clone.clear()
    return clone


def _build(segment: _Segment, plan: list) -> list:
    """New nodes for one run. The originals are copied, never moved, so a later failure changes nothing."""
    top: list = []
    stack: list = []

    def add(node) -> None:
        if stack:
            stack[-1].append(node)
        else:
            top.append(node)

    for item in plan:
        if isinstance(item, str):
            add(NavigableString(item))
            continue
        part = segment.parts[item]
        if part.kind is _PartKind.OPEN:
            clone = _clone_open(part.node)
            add(clone)
            stack.append(clone)
        elif part.kind is _PartKind.CLOSE:
            stack.pop()
        elif part.kind is _PartKind.LEAF:
            add(copy.copy(part.node))
        else:
            add(NavigableString(part.source))
    return top


def _alt_text(segment: _Segment, plan: list) -> str:
    return "".join(item if isinstance(item, str) else segment.parts[item].source for item in plan)


def _results_by_unit(document: ContentDocument, translations: Iterable[UnitTranslation], *, partial: bool) -> dict[str, str]:
    results: dict[str, str] = {}
    for item in translations:
        if type(item) is not UnitTranslation:
            raise HtmlStructureError("invalid_translation")
        if item.unit_id in results:
            raise HtmlStructureError("duplicate_translation")
        results[item.unit_id] = item.text
    units = {unit.unit_id for unit in document.units}
    if results.keys() - units:
        raise HtmlStructureError("unknown_unit")
    if not partial and units - results.keys():
        raise HtmlStructureError("missing_translation")
    return results


def reconstruct_html(html: str | bytes, document: ContentDocument, translations: Iterable[UnitTranslation], *,
                     partial: bool = False) -> bytes:
    """Everything is checked and built before the first change, so a failure returns no document.

    A token only ever means a part of its own unit. A translation with another unit's token
    either names no part here and fails, or restores this unit's own part.
    """
    if type(document) is not ContentDocument:
        raise HtmlStructureError("invalid_document")
    if document.adapter.segmenter_version != HTML_SEGMENTER_VERSION:
        raise HtmlStructureError("incompatible_segmenter")
    if any((unit.content.locator.schema, unit.content.locator.schema_version) != (HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION)
           for unit in document.units):
        raise HtmlStructureError("incompatible_locator")
    results = _results_by_unit(document, translations, partial=partial)
    soup = _parse(html)
    include_alt_text = any(unit.content.kind.name == "alt_text" for unit in document.units)
    segments = _segments(soup, include_alt_text=include_alt_text)
    current = _document(segments, document.project_id, document.logical_key, document.adapter, document.source_locale,
                        document.source_fingerprint)
    if current.revision_id != document.revision_id:
        raise HtmlStructureError("stale_revision")
    staged = []
    for segment, unit in zip(segments, document.units):
        if unit.unit_id not in results:
            continue
        plan = _plan(segment, results[unit.unit_id])
        staged.append((segment, _alt_text(segment, plan) if segment.is_alt else _build(segment, plan)))
    for segment, change in staged:
        if segment.is_alt:
            segment.nodes[0]["alt"] = change
        else:
            replace_run(list(segment.nodes), change)
    return soup.encode(formatter="minimal")
