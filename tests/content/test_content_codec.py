import json

import pytest

import ayvu.content.model as content_module
from ayvu.content.model import ContentSerializationError, ContentValidationError, Extension, decode_document, encode_document
from tests.support.content_fixtures import FINGERPRINT, epub_document

from tests.content.content_support import DOCUMENTS
from tests.content.content_support import _tampered, build, single


# --- Determinism -----------------------------------------------------------------------------------------------------

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
