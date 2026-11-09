"""Synthetic documents in unlike formats, built without importing any parser."""
from __future__ import annotations

from uuid import UUID

from ayvu.content import (
    AdapterIdentity, Constraint, ContextReference, Extension, LocaleState, Locator, ProtectedPart, SemanticKind,
    SourceLocale, UnitContent, UNKNOWN_LOCALE, build_document,
)

PROJECT = str(UUID(int=0x1234))
FINGERPRINT = 'sha256:' + 'c' * 64
ENGLISH = SourceLocale(LocaleState.EXPLICIT, 'en')
EPUB = AdapterIdentity('epub', 1, 1)
SUBTITLE = AdapterIdentity('subtitle', 1, 1)
LAYOUT = AdapterIdentity('layout', 1, 1)


def epub_locator(value: str) -> Locator:
    return Locator('epub_dom_path', 1, value)


def unit(value: str, text: str, kind: str = 'paragraph', order: tuple[int, ...] = (0,), **changes) -> UnitContent:
    return UnitContent(epub_locator(value), text, SemanticKind(kind), order, **changes)


def epub_units() -> list[UnitContent]:
    """A title, a paragraph with inline markup and surrounding spaces, and an image description."""
    paragraph = '  Call <em>me</em> Ishmael.\n'
    title_locator = 'OEBPS/ch1.xhtml#/html/body/h1[1]'
    paragraph_locator = 'OEBPS/ch1.xhtml#/html/body/p[1]'
    return [
        unit(title_locator, 'Loomings', 'title', (0, 0)),
        unit(paragraph_locator, paragraph, 'paragraph', (0, 1),
             protected_parts=(ProtectedPart(7, 11, 'markup'), ProtectedPart(13, 18, 'markup')),
             context=(ContextReference('parent', epub_locator(title_locator)),)),
        unit('OEBPS/ch1.xhtml#/html/body/img[1]/@alt', 'A whale at sea', 'alt_text', (0, 2),
             context=(ContextReference('caption_of', epub_locator(paragraph_locator)),),
             constraints=(Constraint('max_chars', 120),)),
    ]


def subtitle_units() -> list[UnitContent]:
    first, second = Locator('webvtt_cue', 1, 'cue-1'), Locator('webvtt_cue', 1, 'cue-2')
    limits = (Constraint('max_chars', 42), Constraint('max_lines', 2), Constraint('max_duration_ms', 2400))
    return [
        UnitContent(first, 'Where are you going?', SemanticKind('subtitle_cue'), (1,), constraints=limits,
                    context=(ContextReference('next', second),),
                    extensions=(Extension.of('webvtt_timing', 1, {'start_ms': 1000, 'end_ms': 3400}),)),
        UnitContent(second, 'Home.', SemanticKind('subtitle_cue'), (2,), constraints=limits,
                    context=(ContextReference('previous', first),)),
    ]


def layout_units() -> list[UnitContent]:
    return [
        UnitContent(Locator('page_region', 1, 'page-3/region-7'), 'Chapter One', SemanticKind('layout_region'), (3, 7),
                    constraints=(Constraint('max_lines', 1), Constraint('preserve_line_breaks', True)),
                    source_locale=UNKNOWN_LOCALE),
    ]


def epub_document(**changes):
    return build_document(PROJECT, changes.pop('logical_key', 'OEBPS/ch1.xhtml'), changes.pop('adapter', EPUB),
                          changes.pop('source_locale', ENGLISH), changes.pop('units', epub_units()),
                          **{'source_fingerprint': FINGERPRINT, **changes})


def subtitle_document():
    return build_document(PROJECT, 'episode-1.vtt', SUBTITLE, ENGLISH, subtitle_units())


def layout_document():
    return build_document(PROJECT, 'magazine.pdf', LAYOUT, UNKNOWN_LOCALE, layout_units())
