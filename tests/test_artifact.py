from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from ayvu.artifact import (
    ARTIFACT_SCHEMA_VERSION, MAX_ARTIFACT_BYTES, Artifact, ArtifactConflictError, ArtifactRequest, ArtifactState,
    ArtifactValidationError, abandon, mark_ready, new_artifact, ready_location, record_payload, retire,
    reuse_mismatch, staging_location,
)

PROJECT = str(UUID(int=1))
JOB = str(UUID(int=2))
ARTIFACT = str(UUID(int=3))
OTHER = str(UUID(int=4))
FINGERPRINT = 'sha256:' + 'a' * 64
DIGEST = 'sha256:' + 'b' * 64
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


def request(**changes):
    return ArtifactRequest(**{'project_id': PROJECT, 'job_id': JOB, 'role': 'translated_document',
                              'stage': 'translate', 'stage_version': 1, 'input_fingerprint': FINGERPRINT,
                              'media_type': 'application/epub+zip', **changes})


def staged(**changes):
    return replace(new_artifact(request(), ARTIFACT, NOW), **changes) if changes else new_artifact(request(), ARTIFACT, NOW)


def with_payload():
    return record_payload(staged(), 5, DIGEST, expected_revision=1, now=LATER)


def ready():
    return mark_ready(with_payload(), expected_revision=2, now=LATER)


def test_new_artifact_starts_staged_without_payload_metadata():
    artifact = staged()
    assert (artifact.state, artifact.revision, artifact.size, artifact.digest) == (ArtifactState.STAGING, 1, None, None)
    assert artifact.schema_version == ARTIFACT_SCHEMA_VERSION == 1


def test_locations_derive_only_from_the_identity():
    assert staging_location(ARTIFACT) == f'artifacts/staging/{ARTIFACT}.part'
    assert ready_location(ARTIFACT) == f'artifacts/ready/{ARTIFACT}.bin'


@pytest.mark.parametrize('value', ['../../escape', '/etc/passwd', 'a/b', '', None, str(UUID(int=0xabc)).upper()])
def test_locations_reject_anything_but_a_canonical_identity(value):
    for derive in (staging_location, ready_location):
        with pytest.raises(ArtifactValidationError, match='^invalid_identity$'):
            derive(value)


def test_happy_lifecycle_advances_revision_and_time():
    payload = with_payload()
    assert (payload.state, payload.revision, payload.size, payload.digest, payload.updated_at) == (
        ArtifactState.STAGING, 2, 5, DIGEST, LATER)
    done = ready()
    assert (done.state, done.revision) == (ArtifactState.READY, 3)


def test_ready_requires_recorded_payload_metadata():
    with pytest.raises(ArtifactValidationError, match='^ready_requires_payload_metadata$'):
        mark_ready(staged(), expected_revision=1, now=LATER)


def test_payload_is_recorded_once():
    with pytest.raises(ArtifactConflictError, match='^payload_already_recorded$'):
        record_payload(with_payload(), 5, DIGEST, expected_revision=2, now=LATER)


@pytest.mark.parametrize('change', [
    lambda artifact, revision: record_payload(artifact, 5, DIGEST, expected_revision=revision, now=LATER),
    lambda artifact, revision: abandon(artifact, 'cancelled', expected_revision=revision, now=LATER),
])
@pytest.mark.parametrize('revision', [0, 2, True, '1'])
def test_stale_revision_conflicts(change, revision):
    with pytest.raises(ArtifactConflictError, match='^stale_revision$'):
        change(staged(), revision)


@pytest.mark.parametrize('artifact_factory,state', [(ready, 'ready'), (None, 'abandoned')])
def test_only_staging_records_can_be_marked_or_abandoned(artifact_factory, state):
    artifact = artifact_factory() if artifact_factory else abandon(staged(), 'cancelled', expected_revision=1, now=LATER)
    revision = artifact.revision
    for change in (lambda: mark_ready(artifact, expected_revision=revision, now=LATER),
                   lambda: abandon(artifact, 'cancelled', expected_revision=revision, now=LATER),
                   lambda: record_payload(artifact, 5, DIGEST, expected_revision=revision, now=LATER)):
        with pytest.raises(ArtifactConflictError, match='^artifact_not_staging$'):
            change()


def test_retire_applies_only_to_ready_records_and_keeps_payload_metadata():
    retired = retire(ready(), 'integrity_failed', expected_revision=3, now=LATER)
    assert (retired.state, retired.reason_code, retired.size, retired.digest) == (
        ArtifactState.ABANDONED, 'integrity_failed', 5, DIGEST)
    for artifact in (staged(), retired):
        with pytest.raises(ArtifactConflictError, match='^artifact_not_ready$'):
            retire(artifact, 'integrity_failed', expected_revision=artifact.revision, now=LATER)


def test_clock_moving_backwards_does_not_reorder_history():
    moved = record_payload(staged(), 5, DIGEST, expected_revision=1, now=NOW - timedelta(days=1))
    assert moved.updated_at == NOW


def test_abandoned_reason_is_a_required_slug():
    assert abandon(staged(), 'cancelled', expected_revision=1, now=LATER).reason_code == 'cancelled'
    for reason in ('Private Canary Text', '/home/reader/book.epub', '', None):
        with pytest.raises(ArtifactValidationError, match='^invalid_reason$'):
            abandon(staged(), reason, expected_revision=1, now=LATER)
    with pytest.raises(ArtifactValidationError, match='^invalid_reason$'):
        staged(reason_code='cancelled')


@pytest.mark.parametrize('size,digest', [
    (-1, DIGEST), (MAX_ARTIFACT_BYTES + 1, DIGEST), (True, DIGEST), ('5', DIGEST), (5, 'md5:abc'), (5, 'sha256:short'),
    (5, None), (None, DIGEST),
])
def test_invalid_payload_metadata_is_rejected(size, digest):
    with pytest.raises(ArtifactValidationError, match='^invalid_payload_metadata$'):
        staged(size=size, digest=digest)


def test_payload_size_limit_is_inclusive():
    assert staged(size=MAX_ARTIFACT_BYTES, digest=DIGEST).size == MAX_ARTIFACT_BYTES
    assert staged(size=0, digest=DIGEST).size == 0


@pytest.mark.parametrize('version', [0, 2, True, '1'])
def test_unsupported_schema_version_is_distinct(version):
    with pytest.raises(ArtifactValidationError, match='^unsupported_artifact_version$'):
        staged(schema_version=version)


@pytest.mark.parametrize('changes,error', [
    ({'project_id': 'not-a-uuid'}, 'invalid_identity'), ({'job_id': '../job'}, 'invalid_identity'),
    ({'role': 'Private Canary'}, 'invalid_label'), ({'stage': '../stage'}, 'invalid_label'),
    ({'stage_version': 0}, 'invalid_stage_version'), ({'stage_version': True}, 'invalid_stage_version'),
    ({'input_fingerprint': '/home/reader/book.epub'}, 'invalid_fingerprint'),
    ({'media_type': 'text'}, 'invalid_media_type'), ({'media_type': 'Text/Plain'}, 'invalid_media_type'),
    ({'media_type': '../../etc'}, 'invalid_media_type'), ({'media_type': None}, 'invalid_media_type'),
])
def test_invalid_requests_fail_with_bounded_codes(changes, error):
    with pytest.raises(ArtifactValidationError, match=f'^{error}$'):
        request(**changes)


@pytest.mark.parametrize('stamp', [datetime(2026, 11, 3), datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=1))), 'x'])
def test_timestamps_must_be_utc(stamp):
    with pytest.raises(ArtifactValidationError, match='^invalid_timestamp$'):
        new_artifact(request(), ARTIFACT, stamp)


def test_timestamps_must_be_ordered():
    with pytest.raises(ArtifactValidationError, match='^invalid_timestamp_order$'):
        staged(updated_at=NOW - timedelta(seconds=1))


@pytest.mark.parametrize('changes,reason', [
    ({'project_id': OTHER}, 'ownership_mismatch'), ({'job_id': OTHER}, 'ownership_mismatch'),
    ({'role': 'report'}, 'role_mismatch'), ({'media_type': 'text/plain'}, 'role_mismatch'),
    ({'stage': 'validate'}, 'stage_mismatch'), ({'stage_version': 2}, 'stage_mismatch'),
    ({'input_fingerprint': 'sha256:' + 'c' * 64}, 'fingerprint_mismatch'),
])
def test_reuse_requires_every_identity_field_to_match(changes, reason):
    assert reuse_mismatch(ready(), request()) is None
    assert reuse_mismatch(ready(), request(**changes)) == reason


def test_only_ready_records_can_be_reused():
    assert reuse_mismatch(staged(), request()) == 'artifact_not_ready'
    assert reuse_mismatch(with_payload(), request()) == 'artifact_not_ready'
    assert reuse_mismatch(abandon(staged(), 'x', expected_revision=1, now=LATER), request()) == 'artifact_not_ready'


def test_values_are_frozen_and_repr_hides_fingerprint_and_digest():
    artifact = ready()
    with pytest.raises(FrozenInstanceError):
        artifact.state = ArtifactState.STAGING
    text = repr(artifact) + repr(artifact.request)
    assert FINGERPRINT not in text and DIGEST not in text
