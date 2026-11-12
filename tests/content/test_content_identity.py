from dataclasses import replace
import hashlib
import json
import os
import random
import subprocess
import sys

import pytest

import ayvu.content.model as content_module
from ayvu.content.model import CONTENT_SCHEMA_VERSION, IDENTITY_VERSION, UNKNOWN_LOCALE, AdapterIdentity, Constraint, ContentIdentityError, ContentValidationError, ContextLink, ContextReference, Extension, Locator, ProtectedPart, SemanticKind, UnitContent, decode_document, encode_document, source_digest
from ayvu.provenance.model import SubjectRef
from tests.support.content_fixtures import ENGLISH, PROJECT, epub_document, epub_locator, epub_units, unit

from tests.content.content_support import DOCUMENTS, FIXTURES, ID_FORMAT, OTHER_PROJECT
from tests.content.content_support import base_units, build, ids, single
from tests.support.paths import PROJECT_ROOT


# --- Determinism -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('name', DOCUMENTS)
def test_canonical_bytes_are_pinned(name):
    """A failure means the identity or the format changed. Stored IDs would no longer match: add a new version."""
    assert encode_document(DOCUMENTS[name]()) == (FIXTURES / f'{name}.json').read_bytes()


@pytest.mark.parametrize('name', DOCUMENTS)
def test_pinned_bytes_decode_and_encode_back_exactly(name):
    payload = (FIXTURES / f'{name}.json').read_bytes()
    assert encode_document(decode_document(payload)) == payload


def test_separate_processes_produce_identical_bytes_digests_and_ids():
    script = (
        'import hashlib; from tests.support.content_fixtures import *; from ayvu.content.model import encode_document\n'
        'for d in (epub_document(), subtitle_document(), layout_document()):\n'
        '    print(hashlib.sha256(encode_document(d)).hexdigest(), d.revision_id, [u.unit_id for u in d.units],'
        ' [u.source_digest for u in d.units])\n')
    root = PROJECT_ROOT
    outputs = set()
    for seed in ('0', '1', '4242'):
        environment = {**os.environ, 'PYTHONHASHSEED': seed,
                       'PYTHONPATH': os.pathsep.join((str(root), str(root / 'src')))}
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30, env=environment)
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout)
    assert len(outputs) == 1 and outputs.pop().count('\n') == 3


@pytest.mark.parametrize('seed', range(20))
def test_input_order_never_affects_the_document(seed):
    generator = random.Random(seed)
    keys = generator.sample(range(10000), 30)
    units = [unit(f'body/p[{key}]', f'text {key}', order=(key % 7, key)) for key in keys]
    expected = encode_document(build(*sorted(units, key=lambda item: item.order_key)))
    generator.shuffle(units)
    document = build(*units)
    assert encode_document(document) == expected
    assert [item.order_key for item in document.units] == sorted(item.order_key for item in document.units)


def test_list_field_order_never_affects_the_document():
    a, b = Constraint('max_chars', 5), Constraint('max_lines', 1)
    first, second = Extension.of('a', 1, 1), Extension.of('b', 1, 2)
    left = build(single(constraints=(a, b), extensions=(first, second)), extensions=(first, second))
    right = build(single(constraints=(b, a), extensions=(second, first)), extensions=(second, first))
    assert encode_document(left) == encode_document(right)


def test_order_keys_sort_as_positions_and_prefixes_come_first():
    document = build(unit('a', 'x', order=(2,)), unit('b', 'x', order=(1, 0)), unit('c', 'x', order=(1,)),
                     unit('d', 'x', order=(10,)))
    assert [item.order_key for item in document.units] == [(1,), (1, 0), (2,), (10,)]


# --- Identity --------------------------------------------------------------------------------------------------------

def test_equal_text_at_distinct_locators_has_distinct_ids_and_equal_digests():
    document = build(unit('a', 'same', order=(1,)), unit('b', 'same', order=(2,)))
    first, second = document.units
    assert first.unit_id != second.unit_id and first.source_digest == second.source_digest


def test_text_alone_is_not_identity():
    left = build(unit('a', 'same')).units[0].unit_id
    assert left != build(unit('b', 'same')).units[0].unit_id
    assert left != build(unit('a', 'same'), logical_key='chapter-2').units[0].unit_id


@pytest.mark.parametrize('name,change', [
    ('project', lambda: build(*base_units(), project_id=OTHER_PROJECT)),
    ('logical_key', lambda: build(*base_units(), logical_key='chapter-2')),
    ('adapter_namespace', lambda: build(*base_units(), adapter=AdapterIdentity('other', 1, 1))),
    ('adapter_version', lambda: build(*base_units(), adapter=AdapterIdentity('epub', 2, 1))),
    ('segmenter_version', lambda: build(*base_units(), adapter=AdapterIdentity('epub', 1, 2))),
    ('document_locale', lambda: build(*base_units(), source_locale=UNKNOWN_LOCALE)),
    ('unit_locale', lambda: build(*base_units(source_locale=UNKNOWN_LOCALE))),
    ('protected_parts', lambda: build(*base_units(protected_parts=(ProtectedPart(0, 4, 'identifier'),)))),
    ('constraints', lambda: build(*base_units(constraints=(Constraint('max_chars', 9),)))),
    ('context', lambda: build(*base_units(context=(ContextReference('parent', epub_locator('title')),)))),
    ('text', lambda: build(unit('title', 'Title', 'title', (1,)), unit('body', 'Text body!', 'paragraph', (2,)))),
    ('whitespace', lambda: build(unit('title', 'Title', 'title', (1,)), unit('body', 'Text body ', 'paragraph', (2,)))),
    ('kind', lambda: build(unit('title', 'Title', 'heading', (1,)), unit('body', 'Text body', 'paragraph', (2,)))),
    ('order', lambda: build(unit('title', 'Title', 'title', (1,)), unit('body', 'Text body', 'paragraph', (3,)))),
    ('locator_value', lambda: build(unit('title', 'Title', 'title', (1,)), unit('moved', 'Text body', 'paragraph', (2,)))),
    ('locator_schema', lambda: build(unit('title', 'Title', 'title', (1,)), UnitContent(
        Locator('other_schema', 1, 'body'), 'Text body', SemanticKind('paragraph'), (2,)))),
    ('locator_schema_version', lambda: build(unit('title', 'Title', 'title', (1,)), UnitContent(
        Locator('epub_dom_path', 2, 'body'), 'Text body', SemanticKind('paragraph'), (2,)))),
])
def test_every_identity_component_changes_every_unit_id(name, change):
    original, changed = build(*base_units()), change()
    assert changed.revision_id != original.revision_id, name
    assert not set(ids(original)) & set(ids(changed)), name


@pytest.mark.parametrize('document', [
    lambda: build(*base_units(), source_fingerprint='sha256:' + 'd' * 64),
    lambda: build(*base_units(), source_fingerprint=None),
    lambda: build(*base_units(), extensions=(Extension.of('note', 1, {'exported_by': 'tool'}),)),
    lambda: build(*base_units(extensions=(Extension.of('note', 1, [1, 2]),))),
    lambda: build(*reversed(base_units())),
])
def test_extraction_irrelevant_changes_preserve_identity(document):
    original, changed = build(*base_units()), document()
    assert (changed.document_id, changed.revision_id, ids(changed)) == (original.document_id, original.revision_id, ids(original))
    assert [item.source_digest for item in changed.units] == [item.source_digest for item in original.units]


def test_document_id_survives_an_adapter_upgrade_and_a_text_edit():
    original = build(*base_units())
    assert build(*base_units(), adapter=AdapterIdentity('epub', 9, 9)).document_id == original.document_id
    edited = build(unit('title', 'Changed', 'title', (1,)))
    assert edited.document_id == original.document_id and edited.revision_id != original.revision_id


def test_editing_one_document_leaves_another_document_untouched():
    other = build(unit('a', 'Other'), logical_key='chapter-2')
    build(unit('a', 'Edited'), logical_key='chapter-1')
    assert ids(build(unit('a', 'Other'), logical_key='chapter-2')) == ids(other)


def test_digest_is_scoped_to_the_project_and_does_not_normalize():
    assert source_digest(PROJECT, 'text') != source_digest(OTHER_PROJECT, 'text')
    assert source_digest(PROJECT, 'text') != source_digest(PROJECT, 'text ')
    assert source_digest(PROJECT, 'caf\u00e9') != source_digest(PROJECT, 'cafe\u0301')
    assert source_digest(PROJECT, 'text') != 'scd:v1:' + hashlib.sha256(b'text').hexdigest()


def test_hash_parts_are_framed_so_fields_cannot_run_together():
    assert build(unit('ab', 'x'), logical_key='c').units[0].unit_id != build(unit('b', 'x'), logical_key='ca').units[0].unit_id


def test_identity_collision_fails_and_is_not_repaired_with_a_suffix(monkeypatch):
    monkeypatch.setattr(content_module, '_hash', lambda *_: 'a' * 64)
    with pytest.raises(ContentIdentityError, match='^unit_id_collision$'):
        build(unit('a', 'x', order=(1,)), unit('b', 'y', order=(2,)))


def test_constructing_a_document_recomputes_every_id():
    document = build(*base_units())
    wrong_unit = replace(document.units[0], unit_id='unit:v1:' + '0' * 64)
    for change in ({'revision_id': 'rev:v1:' + '0' * 64}, {'document_id': 'doc:v1:' + '0' * 64},
                   {'units': (wrong_unit, document.units[1])},
                   {'units': (replace(document.units[0], source_digest='scd:v1:' + '0' * 64), document.units[1])},
                   {'logical_key': 'other'}, {'project_id': OTHER_PROJECT}):
        with pytest.raises(ContentIdentityError, match='^identity_mismatch$'):
            replace(document, **change)


def test_constructing_a_document_requires_canonical_order():
    document = build(*base_units())
    with pytest.raises(ContentValidationError, match='^invalid_unit_order$'):
        replace(document, units=tuple(reversed(document.units)))
    with pytest.raises(ContentValidationError, match='^invalid_units$'):
        replace(document, units=list(document.units))
    with pytest.raises(ContentValidationError, match='^invalid_units$'):
        replace(document, units=('text',))


def test_context_links_resolve_to_the_target_unit_id():
    document = epub_document()
    title, paragraph, image = document.units
    assert paragraph.context_links == (ContextLink('parent', title.unit_id),)
    assert image.context_links == (ContextLink('caption_of', paragraph.unit_id),)
    assert title.context_links == ()


def test_ids_can_be_used_as_opaque_provenance_subjects():
    for item in epub_document().units:
        assert ID_FORMAT.fullmatch(item.unit_id)
        assert SubjectRef('content_unit', 1, item.unit_id).value == item.unit_id


def test_digest_and_id_are_independent_of_the_review_numbering():
    source = (PROJECT_ROOT / 'src' / 'ayvu' / 'content' / 'model.py').read_text()
    assert 'review_export' not in source and 'parse_segment_id' not in source
    assert not ID_FORMAT.fullmatch('c0001-s0001')


# --- Failures produce no document ------------------------------------------------------------------------------------

def test_versions_are_recorded_on_the_document():
    data = json.loads(encode_document(epub_document()))
    assert data['schema_version'] == CONTENT_SCHEMA_VERSION == 1 and data['identity_version'] == IDENTITY_VERSION == 1
    assert data['adapter'] == {'namespace': 'epub', 'adapter_version': 1, 'segmenter_version': 1}
    assert data['units'][0]['locator']['schema_version'] == 1 and data['units'][0]['kind']['version'] == 1
    assert ENGLISH.value == 'en' and epub_units()[0].kind.name == 'title'


def test_document_id_depends_on_the_project_and_the_adapter_namespace():
    original = build(single())
    assert build(single(), project_id=OTHER_PROJECT).document_id != original.document_id
    assert build(single(), adapter=AdapterIdentity('other', 1, 1)).document_id != original.document_id


def test_locators_differing_only_in_schema_or_version_are_distinct_units():
    kind = SemanticKind('paragraph')
    document = build(UnitContent(Locator('schema_a', 1, 'same'), 'x', kind, (1,)),
                     UnitContent(Locator('schema_b', 1, 'same'), 'x', kind, (2,)),
                     UnitContent(Locator('schema_a', 2, 'same'), 'x', kind, (3,)))
    assert len(set(ids(document))) == 3
