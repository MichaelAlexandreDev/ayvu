
import pytest

from ayvu.knowledge.memory.approved.model import MemorySource, MemoryValidationError
from tests.support.approved_memory_fixtures import document



def test_visible_source_omits_only_structural_markup_and_keeps_stable_content_reference():
    from ayvu.content.model import ProtectedPart
    from ayvu.formats.epub.html_structure import html_unit_template, review_text_from_template
    from tests.support.content_fixtures import epub_document, unit
    text = 'Hello <em>world</em> __AYVU_TAG_0__'
    doc = epub_document(units=[unit('body', text, protected_parts=(
        ProtectedPart(6, 10, 'markup'), ProtectedPart(15, 20, 'markup'),
        ProtectedPart(21, len(text), 'placeholder')))])
    selected = doc.units[0]
    with pytest.raises(MemoryValidationError, match='^visible_source_required$'):
        MemorySource.from_document(doc, selected.unit_id)
    visible = review_text_from_template(html_unit_template(selected),
        [text[part.start:part.end] for part in selected.content.protected_parts])
    src = MemorySource.from_document(doc, selected.unit_id, visible_text=visible)
    assert src.canonical_text == 'Hello world __AYVU_TAG_0__'
    assert src.content_id == doc.units[0].source_digest
    assert '<em>' not in src.search_key and '__ayvu_tag_0__' in src.search_key


def test_format_projection_preserves_breaks_and_omits_hidden_inline_content():
    from ayvu.content.model import ProtectedPart
    from ayvu.formats.epub.html_structure import html_unit_template, review_text_from_template
    from tests.support.content_fixtures import epub_document, unit
    text = 'Hello<br/><script>UNRELATED SECRET</script>world'
    start = text.index('<script>')
    end = text.index('world')
    doc = epub_document(units=[unit('body', text, protected_parts=(
        ProtectedPart(5, start, 'markup'), ProtectedPart(start, end, 'markup')))])
    selected = doc.units[0]
    visible = review_text_from_template(html_unit_template(selected),
        [text[part.start:part.end] for part in selected.content.protected_parts])
    assert visible == 'Hello\nworld'
    src = MemorySource.from_document(doc, selected.unit_id, visible_text=visible)
    assert src.canonical_text == 'Hello\nworld' and src.search_key == 'hello world'
    assert 'UNRELATED SECRET' not in src.canonical_text
    with pytest.raises(MemoryValidationError, match='^visible_source_mismatch$'):
        plain = document('actual')
        MemorySource.from_document(plain, plain.units[0].unit_id, visible_text='unrelated')
