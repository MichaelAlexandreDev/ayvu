from dataclasses import replace
import json

import pytest

from ayvu.project import ProjectError, ProjectManifest, ProjectRequest
from ayvu.project_store import decode_manifest, encode_manifest


def manifest():
    request = ProjectRequest('/private/source.epub', 10, 'a' * 64, 'en', 'pt')
    return ProjectManifest.create(request, '12345678-1234-4234-8234-123456789abc')


def test_manifest_canonical_round_trip_and_redacted_representation():
    value = manifest()
    assert decode_manifest(encode_manifest(value)) == value
    assert encode_manifest(value).endswith(b'\n')
    assert encode_manifest(value) == (json.dumps(
        json.loads(encode_manifest(value)), ensure_ascii=False, sort_keys=True,
        separators=(',', ':')) + '\n').encode()
    assert '/private' not in repr(value)
    assert 'a' * 64 not in repr(value)
    assert '/private' not in repr(value.request())


@pytest.mark.parametrize('change', [
    {'schema_version': 2}, {'revision': True}, {'project_id': '../escape'},
    {'api_key': 'secret-canary'}, {'locations': ['../escape']},
    {'locations': ['/absolute']}, {'locations': ['reports', 'reports/../reports']},
    {'provider_ref': 'https://user:secret@host'},
    {'source_size': True}, {'metadata': {'api_key': 'secret-canary'}},
    {'created_at': 'no-date'}, {'source': []},
    {'source_path': 'relative/source.epub'},
])
def test_manifest_rejects_untrusted_schema_without_echoing_values(change):
    data = json.loads(encode_manifest(manifest()))
    data.update(change)
    with pytest.raises(ProjectError) as caught:
        decode_manifest(json.dumps(data).encode())
    assert 'secret' not in str(caught.value)
    assert '/private' not in str(caught.value)


def test_immutable_manifest_validation():
    with pytest.raises(ProjectError):
        replace(manifest(), locations=('reports', 'reports'))


def test_unknown_duplicate_and_oversized_fields():
    for payload in (b'{"schema_version":1,"schema_version":1}', b'[]', b'{' * 40000):
        with pytest.raises(ProjectError):
            decode_manifest(payload)
