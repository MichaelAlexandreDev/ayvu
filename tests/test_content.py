import ast
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import subprocess
import sys
from uuid import UUID

import pytest

import ayvu.content as content_module
from ayvu.content import (
    CONTENT_SCHEMA_VERSION, IDENTITY_VERSION, INHERITED_LOCALE, MAX_ORDER_DEPTH, MAX_REFERENCE_CHARS, UNKNOWN_LOCALE,
    AdapterIdentity, Constraint, ContentDocument, ContentIdentityError, ContentSerializationError,
    ContentValidationError, ContentVersionError, ContextLink, ContextReference, Extension, LocaleState, Locator,
    ProtectedPart, SemanticKind, SourceLocale, TranslationUnit, UnitContent, build_document, decode_document,
    encode_document, source_digest,
)
from ayvu.provenance import SubjectRef
from content_fixtures import (
    ENGLISH, EPUB, FINGERPRINT, PROJECT, epub_document, epub_locator, epub_units, layout_document, subtitle_document, unit,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'content'
OTHER_PROJECT = str(UUID(int=0x9999))
DOCUMENTS = {'epub': epub_document, 'subtitle': subtitle_document, 'layout': layout_document}
ID_FORMAT = re.compile(r'unit:v1:[0-9a-f]{64}')


def ids(document):
    return [item.unit_id for item in document.units]


def single(text='Hello', **changes):
    return unit('body/p[1]', text, **changes)


def build(*units, **changes):
    return build_document(changes.pop('project_id', PROJECT), changes.pop('logical_key', 'chapter-1'),
                          changes.pop('adapter', EPUB), changes.pop('source_locale', ENGLISH), units, **changes)


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
        'import hashlib; from content_fixtures import *; from ayvu.content import encode_document\n'
        'for d in (epub_document(), subtitle_document(), layout_document()):\n'
        '    print(hashlib.sha256(encode_document(d)).hexdigest(), d.revision_id, [u.unit_id for u in d.units],'
        ' [u.source_digest for u in d.units])\n')
    root = Path(__file__).parents[1]
    outputs = set()
    for seed in ('0', '1', '4242'):
        environment = {**os.environ, 'PYTHONHASHSEED': seed,
                       'PYTHONPATH': os.pathsep.join((str(root / 'src'), str(root / 'tests')))}
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


def base_units(**changes):
    return [unit('title', 'Title', 'title', (1,)), unit('body', 'Text body', 'paragraph', (2,), **changes)]


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
    source = (Path(__file__).parents[1] / 'src' / 'ayvu' / 'content.py').read_text()
    assert 'review_export' not in source and 'parse_segment_id' not in source
    assert not ID_FORMAT.fullmatch('c0001-s0001')


# --- Failures produce no document ------------------------------------------------------------------------------------

@pytest.mark.parametrize('units,code,index', [
    ((unit('a', 'x', order=(1,)), unit('a', 'y', order=(2,))), 'duplicate_locator', 1),
    ((unit('a', 'x', order=(1,)), unit('b', 'y', order=(1,))), 'duplicate_order_key', 1),
    ((unit('a', 'x', order=(1,), context=(ContextReference('parent', epub_locator('missing')),)),), 'missing_context_reference', 0),
    ((unit('a', 'x', order=(1,)), unit('b', 'y', order=(2,), context=(ContextReference('parent', epub_locator('b')),))), 'self_context_reference', 1),
])
def test_invalid_documents_fail_with_structured_errors(units, code, index):
    with pytest.raises(ContentValidationError, match=f'^{code}$') as caught:
        build(*units)
    assert caught.value.code == code and caught.value.unit_index == index


def test_values_that_are_not_units_are_refused_with_their_position():
    with pytest.raises(ContentValidationError, match='^invalid_units$') as caught:
        build(single(), 'text')
    assert caught.value.unit_index == 1


def test_context_reference_in_another_locator_schema_does_not_match():
    with pytest.raises(ContentValidationError, match='^missing_context_reference$'):
        build(unit('a', 'x', order=(1,)), unit('b', 'y', order=(2,), context=(
            ContextReference('parent', Locator('other_schema', 1, 'a')),)))


@pytest.mark.parametrize('order', [(), [1], (1.0,), (True,), (-1,), ('1',), None, (2**63,), tuple(range(MAX_ORDER_DEPTH + 1))], ids=repr)
def test_invalid_order_keys_are_refused(order):
    with pytest.raises(ContentValidationError, match='^invalid_order_key$'):
        single(order=order)


@pytest.mark.parametrize('parts,code', [
    ((ProtectedPart(0, 4, 'markup'), ProtectedPart(3, 5, 'markup')), 'overlapping_protected_parts'),
    ((ProtectedPart(2, 4, 'markup'), ProtectedPart(0, 1, 'markup')), 'overlapping_protected_parts'),
    ((ProtectedPart(0, 2, 'markup'), ProtectedPart(0, 2, 'url')), 'overlapping_protected_parts'),
    ((ProtectedPart(0, 6, 'markup'),), 'invalid_protected_part'),
    ([ProtectedPart(0, 1, 'markup')], 'invalid_protected_part'),
    (('x',), 'invalid_protected_part'),
])
def test_invalid_protected_parts_are_refused(parts, code):
    with pytest.raises(ContentValidationError, match=f'^{code}$'):
        single(protected_parts=parts)


def test_adjacent_protected_parts_and_a_span_reaching_the_end_are_valid():
    item = single(protected_parts=(ProtectedPart(0, 2, 'markup'), ProtectedPart(2, 5, 'url')))
    assert len(item.protected_parts) == 2


@pytest.mark.parametrize('start,end', [(-1, 2), (2, 2), (3, 2), (True, 2), (0, 1.0), ('0', 1)])
def test_invalid_spans_are_refused(start, end):
    with pytest.raises(ContentValidationError, match='^invalid_protected_part$'):
        ProtectedPart(start, end, 'markup')


def test_spans_count_code_points():
    text = '\U0001f40b whale'
    item = unit('a', text, protected_parts=(ProtectedPart(0, 1, 'identifier'),))
    assert text[item.protected_parts[0].start:item.protected_parts[0].end] == '\U0001f40b'
    assert decode_document(encode_document(build(item))).units[0].content.protected_parts == item.protected_parts


@pytest.mark.parametrize('name,value', [('max_chars', 0), ('max_chars', True), ('max_chars', '5'), ('max_lines', -1),
                                        ('max_duration_ms', 1.5), ('preserve_line_breaks', 1), ('max_chars', 2**63)])
def test_invalid_constraint_values_are_refused(name, value):
    with pytest.raises(ContentValidationError, match='^invalid_constraint$'):
        Constraint(name, value)


def test_duplicate_constraints_contexts_and_extensions_are_refused():
    reference = ContextReference('parent', epub_locator('x'))
    with pytest.raises(ContentValidationError, match='^invalid_constraint$'):
        single(constraints=(Constraint('max_chars', 1), Constraint('max_chars', 2)))
    with pytest.raises(ContentValidationError, match='^invalid_context_reference$'):
        single(context=(reference, reference))
    with pytest.raises(ContentValidationError, match='^invalid_extension$'):
        single(extensions=(Extension.of('n', 1, 1), Extension.of('n', 1, 2)))
    with pytest.raises(ContentValidationError, match='^invalid_constraint$'):
        single(constraints=('x',))


@pytest.mark.parametrize('build_value,code', [
    (lambda: SemanticKind('unknown_kind'), 'unsupported_semantic_kind'),
    (lambda: SemanticKind('paragraph', 2), 'unsupported_semantic_kind'),
    (lambda: Constraint('unknown_limit', 1), 'unsupported_constraint'),
    (lambda: Constraint('max_chars', 1, 2), 'unsupported_constraint'),
    (lambda: ProtectedPart(0, 1, 'unknown_kind'), 'unsupported_protected_kind'),
    (lambda: ProtectedPart(0, 1, None), 'unsupported_protected_kind'),
    (lambda: ContextReference('unknown_relation', epub_locator('a')), 'unsupported_context_relation'),
    (lambda: Extension('anything', 1, '{}', True), 'unsupported_required_extension'),
])
def test_unsupported_required_versions_fail(build_value, code):
    with pytest.raises(ContentVersionError, match=f'^{code}$'):
        build_value()


@pytest.mark.parametrize('field,value,code', [
    ('schema_version', 2, 'unsupported_content_schema'), ('schema_version', 0, 'unsupported_content_schema'),
    ('identity_version', 2, 'unsupported_identity_version'),
])
def test_unsupported_document_versions_fail_distinctly(field, value, code):
    document = epub_document()
    data = json.loads(encode_document(document))
    data[field] = value
    with pytest.raises(ContentVersionError, match=f'^{code}$'):
        decode_document(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
    with pytest.raises(ContentVersionError, match=f'^{code}$'):
        replace(document, **{field: value})


@pytest.mark.parametrize('path,value,code', [
    (('units', 0, 'kind', 'version'), 2, 'unsupported_semantic_kind'),
    (('units', 0, 'kind', 'name'), 'unknown', 'unsupported_semantic_kind'),
    (('units', 2, 'constraints', 0, 'name'), 'unknown', 'unsupported_constraint'),
])
def test_unsupported_nested_versions_fail_when_decoding(path, value, code):
    data = json.loads(encode_document(epub_document()))
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ContentVersionError, match=f'^{code}$'):
        decode_document(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())


@pytest.mark.parametrize('text', ['', None, 7, b'bytes', '\ud800'], ids=repr)
def test_invalid_source_text_is_refused(text):
    with pytest.raises(ContentValidationError, match='^invalid_source_text$'):
        single(text=text)


def test_source_text_size_is_bounded(monkeypatch):
    monkeypatch.setattr(content_module, 'MAX_SOURCE_CHARS', 4)
    with pytest.raises(ContentValidationError, match='^invalid_source_text$'):
        single(text='12345')


def test_unit_count_is_bounded(monkeypatch):
    monkeypatch.setattr(content_module, 'MAX_UNITS', 1)
    with pytest.raises(ContentValidationError, match='^too_many_units$'):
        build(unit('a', 'x', order=(1,)), unit('b', 'y', order=(2,)))


def test_document_size_is_bounded(monkeypatch):
    document = epub_document()
    monkeypatch.setattr(content_module, 'MAX_DOCUMENT_BYTES', 16)
    with pytest.raises(ContentSerializationError, match='^document_too_large$'):
        encode_document(document)
    with pytest.raises(ContentSerializationError, match='^invalid_document$'):
        decode_document(b'x' * 17)


@pytest.mark.parametrize('value', [
    '', None, 7, '/home/reader/book.epub', '\\\\server\\share', 'C:\\Users\\reader\\book.epub', 'c:/reader/book.epub',
    'https://user:hunter2@example.test/x', 'line\nbreak', 'tab\there', '\x7f', '\ud800', 'x' * (MAX_REFERENCE_CHARS + 1),
], ids=repr)
def test_locators_and_logical_keys_cannot_be_absolute_paths_or_urls(value):
    with pytest.raises(ContentValidationError, match='^invalid_locator$'):
        Locator('schema', 1, value)
    with pytest.raises(ContentValidationError, match='^invalid_logical_key$'):
        build(single(), logical_key=value)


@pytest.mark.parametrize('value', ['OEBPS/ch1.xhtml#/html/body/p[2]', 'cue-1', 'página 3', 'a b', 'page-3/region-7'])
def test_locator_values_are_otherwise_opaque(value):
    assert decode_document(encode_document(build(unit(value, 'x')))).units[0].locator.value == value


@pytest.mark.parametrize('changes,code', [
    ({'project_id': 'not-a-uuid'}, 'invalid_project_identity'), ({'project_id': str(UUID(int=0xabc)).upper()}, 'invalid_project_identity'),
    ({'adapter': 'epub'}, 'invalid_adapter'), ({'source_locale': 'en'}, 'invalid_locale'),
    ({'source_locale': INHERITED_LOCALE}, 'invalid_locale'), ({'source_fingerprint': 'md5:abc'}, 'invalid_source_fingerprint'),
    ({'source_fingerprint': '/home/reader/book.epub'}, 'invalid_source_fingerprint'), ({'extensions': ('x',)}, 'invalid_extension'),
])
def test_invalid_document_inputs_are_refused(changes, code):
    with pytest.raises(ContentValidationError, match=f'^{code}$'):
        build(single(), **changes)


@pytest.mark.parametrize('namespace,adapter_version,segmenter_version', [('Bad Name', 1, 1), ('epub', 0, 1), ('epub', 1, True), ('epub', '1', 1)])
def test_invalid_adapter_identity_is_refused(namespace, adapter_version, segmenter_version):
    with pytest.raises(ContentValidationError, match='^invalid_adapter$'):
        AdapterIdentity(namespace, adapter_version, segmenter_version)


@pytest.mark.parametrize('state,value', [(LocaleState.EXPLICIT, None), (LocaleState.EXPLICIT, 'not a locale'), (LocaleState.EXPLICIT, '/x'),
                                         (LocaleState.UNKNOWN, 'en'), (LocaleState.INHERITED, 'en'), ('explicit', 'en')])
def test_invalid_locales_are_refused(state, value):
    with pytest.raises(ContentValidationError, match='^invalid_locale$'):
        SourceLocale(state, value)


@pytest.mark.parametrize('locale', [SourceLocale(LocaleState.EXPLICIT, 'pt-BR'), INHERITED_LOCALE, UNKNOWN_LOCALE], ids=repr)
def test_every_locale_state_round_trips(locale):
    decoded = decode_document(encode_document(build(single(source_locale=locale)))).units[0].content.source_locale
    assert decoded == locale


def test_unit_has_no_target_locale_or_translation_state():
    names = {field.name for field in fields(UnitContent)} | {field.name for field in fields(TranslationUnit)}
    assert not {'target_locale', 'state', 'translation', 'translated_text', 'status', 'review_state'} & names


# --- Round trip ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    '  leading', 'trailing  ', '\t\n mixed \r\n', ' ', '\n', '\u00a0non breaking\u00a0', 'zero\u200bwidth',
    'caf\u00e9', 'cafe\u0301', '\U0001f40b', '\u05d0\u05d1\u05d2', 'a\u2028b\u2029c', '"quotes" \\ back/slash',
    '\x00\x01control', '<em>markup</em> &amp;',
], ids=repr)
def test_exact_source_text_survives_the_round_trip(text):
    document = build(single(text=text))
    decoded = decode_document(encode_document(document))
    assert decoded == document and decoded.units[0].source_text == text


def test_every_field_survives_the_round_trip():
    for factory in DOCUMENTS.values():
        document = factory()
        decoded = decode_document(encode_document(document))
        assert decoded == document
        assert decoded.units == document.units and decoded.source_fingerprint == document.source_fingerprint
    assert epub_document().source_fingerprint == FINGERPRINT


def test_unknown_optional_extensions_round_trip_verbatim():
    payload = {'z': [1, 2.5, None, True, {'nested': 'caf\u00e9'}], 'a': 'value'}
    extension = Extension.of('future_feature', 7, payload)
    document = build(single(extensions=(extension,)), extensions=(Extension.of('other', 99, ['x']),))
    decoded = decode_document(encode_document(document))
    assert decoded.units[0].content.extensions == (extension,)
    assert json.loads(decoded.units[0].content.extensions[0].payload_json) == payload
    assert decoded.extensions[0].version == 99


@pytest.mark.parametrize('payload', ['not json', '{"a": 1}', '{"b":1,"a":2}', '[1,2', None, 7, '[NaN]'], ids=repr)
def test_extension_payload_must_be_canonical_json(payload):
    with pytest.raises(ContentValidationError, match='^invalid_extension$'):
        Extension('namespace', 1, payload)


@pytest.mark.parametrize('value', [float('nan'), object(), {1: 'x'}.keys(), b'bytes'], ids=['nan', 'object', 'keys', 'bytes'])
def test_extension_value_must_be_json(value):
    with pytest.raises(ContentValidationError, match='^invalid_extension$'):
        Extension.of('namespace', 1, value)


def test_extension_size_is_bounded(monkeypatch):
    monkeypatch.setattr(content_module, 'MAX_EXTENSION_BYTES', 4)
    with pytest.raises(ContentValidationError, match='^invalid_extension$'):
        Extension.of('namespace', 1, 'too long')


@pytest.mark.parametrize('namespace,version,required', [('Bad Name', 1, False), ('n', 0, False), ('n', True, False), ('n', 1, 1)])
def test_invalid_extension_headers_are_refused(namespace, version, required):
    with pytest.raises(ContentValidationError, match='^invalid_extension$'):
        Extension(namespace, version, '{}', required)


def _tampered(mutate):
    data = json.loads(encode_document(epub_document()))
    mutate(data)
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('mutate', [
    lambda data: data.update(extra=1),
    lambda data: data.pop('units'),
    lambda data: data.update(document_id='doc:v1:' + '0' * 64),
    lambda data: data.update(revision_id='rev:v1:' + '0' * 64),
    lambda data: data['units'][0].update(unit_id='unit:v1:' + '0' * 64),
    lambda data: data['units'][0].update(source_digest='scd:v1:' + '0' * 64),
    lambda data: data['units'][0].update(source_text='Edited'),
    lambda data: data['units'][0].update(extra=1),
    lambda data: data['units'].reverse(),
    lambda data: data['units'][1]['protected_parts'].reverse(),
    lambda data: data['units'][1]['protected_parts'][0].update(end=99),
    lambda data: data.update(units='text'),
    lambda data: data['units'][0].update(order_key='text'),
    lambda data: data['units'][1]['context'][0]['target'].update(value='missing'),
], ids=range(14))
def test_decode_rejects_any_tampering(mutate):
    with pytest.raises(ContentSerializationError, match='^invalid_document$'):
        decode_document(_tampered(mutate))


@pytest.mark.parametrize('payload', [b'', b'not json', b'[]', b'{}', b'[' * 200000, 'text', None, '{"a":1,"a":2}'.encode(),
                                     '{"x":NaN}'.encode(), b'\xff\xfe', 'caf\u00e9'.encode()], ids=range(11))
def test_decode_rejects_malformed_input(payload):
    with pytest.raises(ContentSerializationError, match='^invalid_document$'):
        decode_document(payload)


def test_decode_rejects_valid_json_in_a_non_canonical_form():
    payload = encode_document(epub_document())
    for variant in (payload + b' ', json.dumps(json.loads(payload)).encode(),
                    json.dumps(json.loads(payload), sort_keys=True, ensure_ascii=False).encode()):
        with pytest.raises(ContentSerializationError, match='^invalid_document$'):
            decode_document(variant)


# --- Privacy and boundaries ------------------------------------------------------------------------------------------

CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'https://user:hunter2@example.test/x', 'sk-secret-token', 'C:\\Users\\reader\\book.epub')


@pytest.mark.parametrize('canary', CANARIES)
def test_text_paths_and_credentials_never_reach_ids_repr_or_errors(canary, caplog):
    caplog.set_level(logging.DEBUG)
    document = build(unit('body/p[1]', canary, protected_parts=(ProtectedPart(0, 4, 'identifier'),),
                          constraints=(Constraint('max_chars', 999),), extensions=(Extension.of('note', 1, canary),),
                          order=(1,)),
                     unit('body/p[2]', 'other', order=(2,), context=(ContextReference('previous', epub_locator('body/p[1]')),)))
    first, second = document.units
    values = [document, first, second, first.content, first.locator, first.content.extensions[0], first.content.protected_parts[0],
              first.content.constraints[0], second.content.context[0], second.context_links[0], document.adapter]
    texts = [repr(value) for value in values] + [str(value) for value in values]
    texts += [document.document_id, document.revision_id, first.unit_id, second.unit_id, first.source_digest]
    for attempt in (lambda: Locator('schema', 1, canary + '\n'), lambda: SemanticKind(canary),
                    lambda: Constraint(canary, 1), lambda: ContextReference(canary, epub_locator('a')),
                    lambda: Extension(canary, 1, '{}'), lambda: AdapterIdentity(canary, 1, 1),
                    lambda: SourceLocale(LocaleState.EXPLICIT, canary + ' '), lambda: build(single(), project_id=canary),
                    lambda: build(single(), source_fingerprint=canary), lambda: decode_document(canary.encode())):
        with pytest.raises(ValueError) as caught:
            attempt()
        texts += [str(caught.value), repr(caught.value)]
    texts.append(caplog.text)
    assert not any(canary in text for text in texts)


def test_repr_hides_locators_digests_and_keys():
    document = epub_document()
    text = repr(document) + repr(document.units[1]) + repr(document.units[1].locator)
    for secret in ('OEBPS/ch1.xhtml', document.units[1].source_digest, FINGERPRINT, PROJECT):
        assert secret not in text


def test_errors_carry_only_a_code_and_a_position():
    with pytest.raises(ContentValidationError) as caught:
        build(unit('a', 'secret text', order=(1,)), unit('a', 'secret text', order=(2,)))
    assert vars(caught.value) == {'code': 'duplicate_locator', 'unit_index': 1}


def test_values_are_frozen():
    document = epub_document()
    with pytest.raises(FrozenInstanceError):
        document.revision_id = 'x'
    with pytest.raises(FrozenInstanceError):
        document.units[0].unit_id = 'x'
    with pytest.raises(FrozenInstanceError):
        document.units[0].content.source_text = 'x'
    assert type(document.units) is tuple and type(document.units[1].content.protected_parts) is tuple


def test_model_imports_only_the_standard_library():
    tree = ast.parse((Path(__file__).parents[1] / 'src' / 'ayvu' / 'content.py').read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, 'the content model imports no other Ayvu module'
            roots.add(node.module.split('.')[0])
    assert roots <= sys.stdlib_module_names
    assert not roots & {'os', 'pathlib', 'sqlite3', 'html', 'xml', 'zipfile'}


def test_unlike_formats_share_the_model_without_format_fields():
    kinds = {name: {item.content.kind.name for item in factory().units} for name, factory in DOCUMENTS.items()}
    assert kinds == {'epub': {'title', 'paragraph', 'alt_text'}, 'subtitle': {'subtitle_cue'}, 'layout': {'layout_region'}}
    names = {field.name for field in fields(UnitContent)}
    assert not {'html', 'tag', 'dom', 'node', 'cue', 'timestamp', 'bbox', 'page'} & names


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
