"""EPUB: the HTML structure rules. Reading the archive, the format adapter, the EPUB reader and the output
validator are adapter modules.
"""
from __future__ import annotations

from .html_structure import (
    IGNORED_INLINE_TAGS, IGNORED_TAGS, INLINE_TAGS, TransientHtmlDocument, extract_visible_text,
    has_translatable_text, html_unit_template, normalize_translated_text, review_text_from_template,
    without_xml_invalid,
)

__all__ = [
    "IGNORED_INLINE_TAGS", "IGNORED_TAGS", "INLINE_TAGS", "TransientHtmlDocument", "extract_visible_text",
    "has_translatable_text", "html_unit_template", "normalize_translated_text", "review_text_from_template",
    "without_xml_invalid",
]
