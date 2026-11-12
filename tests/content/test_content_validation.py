from dataclasses import fields, replace
import json
from uuid import UUID

import pytest

import ayvu.content.model as content_module
from ayvu.content.model import INHERITED_LOCALE, MAX_ORDER_DEPTH, MAX_REFERENCE_CHARS, UNKNOWN_LOCALE, AdapterIdentity, Constraint, ContentSerializationError, ContentValidationError, ContentVersionError, ContextReference, Extension, LocaleState, Locator, ProtectedPart, SemanticKind, SourceLocale, TranslationUnit, UnitContent, decode_document, encode_document
from tests.support.content_fixtures import epub_document, epub_locator, unit

from tests.content.content_support import build, single


# --- Determinism -----------------------------------------------------------------------------------------------------

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
