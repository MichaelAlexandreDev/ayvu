import errno
import os
from contextlib import closing
import sqlite3
from uuid import UUID

import pytest

from ayvu.artifacts.model import ArtifactConflictError, ArtifactIntegrityError, ArtifactNotFoundError, ArtifactPathError, ArtifactState, ArtifactStoreError, ArtifactValidationError

from tests.artifacts.artifact_service_support import FaultOps
from tests.artifacts.artifact_service_support import OTHER_PROJECT, PAYLOAD
from tests.artifacts.artifact_service_support import SimulatedCrash, _tamper, env


@pytest.mark.parametrize('changes', [
    {'role': 'report'}, {'stage': 'validate'}, {'stage_version': 2}, {'input_fingerprint': 'sha256:' + 'c' * 64},
    {'job_id': str(UUID(int=77))},
])
def test_different_identity_finds_nothing_to_reuse(env, changes):
    env.publish()
    result = env.service().find_reusable(env.request(**changes))
    assert result.artifact is None and result.reason == 'artifact_not_found'


@pytest.mark.parametrize('changes,reason', [
    ({'role': 'report'}, 'role_mismatch'), ({'media_type': 'text/plain'}, 'role_mismatch'),
    ({'stage': 'validate'}, 'stage_mismatch'), ({'stage_version': 2}, 'stage_mismatch'),
    ({'input_fingerprint': 'sha256:' + 'c' * 64}, 'fingerprint_mismatch'),
    ({'job_id': str(UUID(int=77))}, 'ownership_mismatch'), ({'project_id': OTHER_PROJECT}, 'ownership_mismatch'),
])
def test_opening_by_identifier_refuses_any_mismatch(env, changes, reason):
    artifact = env.publish()
    with pytest.raises(ArtifactConflictError, match=f'^{reason}$'):
        env.service().open_reusable(artifact.artifact_id, env.request(**changes))


def test_media_type_mismatch_in_lookup_is_refused(env):
    env.publish()
    result = env.service().find_reusable(env.request(media_type='text/plain'))
    assert result.artifact is None and result.reason == 'role_mismatch'


def test_unknown_artifact_and_invalid_identity_are_typed(env):
    with pytest.raises(ArtifactNotFoundError, match='^artifact_not_found$'):
        env.service().open_reusable(str(UUID(int=77)), env.request())
    with pytest.raises(ArtifactValidationError, match='^invalid_identity$'):
        env.service().open_reusable('../../etc/passwd', env.request())


@pytest.mark.parametrize('corrupt,reason', [
    (lambda path: path.write_bytes(b'x'), 'payload_mismatch'),
    (lambda path: path.write_bytes(b'Z' + b''.join(PAYLOAD)[1:]), 'payload_mismatch'),
    (lambda path: path.unlink(), 'payload_missing'),
])
def test_existence_alone_never_proves_validity(env, corrupt, reason):
    artifact = env.publish()
    corrupt(env.final_path(artifact.artifact_id))
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == reason
    with pytest.raises(ArtifactIntegrityError, match=f'^{reason}$'):
        env.service().open_reusable(artifact.artifact_id, env.request())
    assert env.rows()[0][1] == 'ready'


def test_corrupt_artifact_is_retired_only_on_request_and_work_can_be_redone(env):
    artifact = env.publish()
    path = env.final_path(artifact.artifact_id)
    path.write_bytes(b'corrupted')
    assert env.rows()[0][1] == 'ready'
    with pytest.raises(ArtifactConflictError, match='^artifact_exists$'):
        env.publish()
    retired = env.service().retire_if_corrupt(artifact.artifact_id)
    assert (retired.state, retired.reason_code) == (ArtifactState.ABANDONED, 'integrity_failed')
    assert path.read_bytes() == b'corrupted'
    redone = env.publish()
    assert redone.artifact_id != artifact.artifact_id
    assert env.service().find_reusable(env.request()).artifact == redone


def test_retire_leaves_valid_and_non_ready_records_alone(env):
    artifact = env.publish()
    assert env.service().retire_if_corrupt(artifact.artifact_id) == artifact
    with pytest.raises(ArtifactStoreError):
        env.publish(env.service(FaultOps('write', OSError(errno.EIO, 'x'))), stage_version=2)
    abandoned_id = env.rows()[1][0] if env.rows()[1][1] == 'abandoned' else env.rows()[0][0]
    assert env.service().retire_if_corrupt(abandoned_id).state is ArtifactState.ABANDONED


def test_ready_directory_symlink_is_refused_without_writing_outside(env):
    outside = env.tmp_path / 'outside'
    outside.mkdir()
    env.root.mkdir()
    (env.root / 'ready').symlink_to(outside)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert list(outside.iterdir()) == []
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'unsafe_location')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('link_name', ['artifacts', 'artifacts/staging'])
def test_root_and_staging_symlinks_are_refused_without_writing_outside(env, link_name):
    outside = env.tmp_path / 'outside'
    outside.mkdir()
    link = env.tmp_path / link_name
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert list(outside.iterdir()) == []
    assert env.rows()[0][1:3] == ('abandoned', 'symlink_path')


def test_staging_path_that_is_already_a_symlink_is_not_followed(env):
    env.next_id = 700
    victim = env.tmp_path / 'victim'
    victim.write_bytes(b'unrelated')
    (env.root / 'staging').mkdir(parents=True)
    env.staged_path(str(UUID(int=701))).symlink_to(victim)
    with pytest.raises(ArtifactPathError, match='^symlink_path$'):
        env.publish()
    assert victim.read_bytes() == b'unrelated'


def test_existing_file_at_the_staging_path_is_not_replaced(env):
    env.next_id = 700
    (env.root / 'staging').mkdir(parents=True)
    existing = env.staged_path(str(UUID(int=701)))
    existing.write_bytes(b'unrelated')
    with pytest.raises(ArtifactConflictError, match='^staging_path_exists$'):
        env.publish()
    assert existing.read_bytes() == b'unrelated'


def test_ready_path_collision_never_overwrites_and_reconciles_as_mismatch(env):
    env.next_id = 700
    (env.root / 'ready').mkdir(parents=True)
    existing = env.final_path(str(UUID(int=701)))
    existing.write_bytes(b'unrelated')
    with pytest.raises(ArtifactConflictError, match='^ready_path_collision$'):
        env.publish()
    assert existing.read_bytes() == b'unrelated'
    assert env.rows()[0][1] == 'staging'
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'final_mismatch')
    assert existing.read_bytes() == b'unrelated'
    assert env.staged_path(str(UUID(int=701))).read_bytes() == b''.join(PAYLOAD)


def test_symlink_swapped_in_after_publication_is_not_followed(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    outside = env.tmp_path / 'outside.bin'
    outside.write_bytes(b''.join(PAYLOAD))
    final.unlink()
    final.symlink_to(outside)
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'symlink_path'
    assert env.service().retire_if_corrupt(artifact.artifact_id).state is ArtifactState.ABANDONED
    assert outside.read_bytes() == b''.join(PAYLOAD)


def test_hardlinked_final_file_is_refused_even_with_matching_content(env):
    artifact = env.publish()
    os.link(env.final_path(artifact.artifact_id), env.tmp_path / 'alias.bin')
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'payload_has_aliases'
    assert env.service().retire_if_corrupt(artifact.artifact_id).state is ArtifactState.ABANDONED


def test_foreign_hardlink_is_not_taken_for_an_interrupted_publication(env):
    with pytest.raises(SimulatedCrash):
        env.publish(env.service(FaultOps('sync_directory:ready')))
    artifact_id = env.rows()[0][0]
    # The staged name no longer refers to the final file: its bytes are a separate copy.
    staged = env.staged_path(artifact_id)
    staged.unlink()
    staged.write_bytes(b''.join(PAYLOAD))
    os.link(env.final_path(artifact_id), env.tmp_path / 'alias.bin')
    env.expire_lease()
    env.service().reconcile()
    assert env.rows()[0][1:3] == ('abandoned', 'final_mismatch')
    assert staged.exists()


def test_directory_at_the_final_path_is_refused(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    final.unlink()
    final.mkdir()
    result = env.service().find_reusable(env.request())
    assert result.artifact is None and result.reason == 'not_a_regular_file'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO')
def test_fifo_at_the_final_path_does_not_block(env):
    artifact = env.publish()
    final = env.final_path(artifact.artifact_id)
    final.unlink()
    os.mkfifo(final)
    assert env.service().find_reusable(env.request()).reason == 'not_a_regular_file'


@pytest.mark.parametrize('location', ['../../../etc/passwd', '/etc/passwd', 'artifacts/ready/other.bin', '', 'artifacts/ready/../../project.sqlite'])
def test_tampered_stored_location_is_rejected_without_touching_the_path(env, location):
    artifact = env.publish()
    _tamper(env, f"location = '{location}'")
    with pytest.raises(ArtifactPathError, match='^invalid_location$'):
        env.service().find_reusable(env.request())
    with pytest.raises(ArtifactPathError, match='^invalid_location$'):
        env.service().open_reusable(artifact.artifact_id, env.request())


@pytest.mark.parametrize('assignment,error,kind', [
    ('schema_version = 2', 'unsupported_artifact_version', ArtifactValidationError),
    ("state = 'unknown'", 'invalid_artifact_record', ArtifactStoreError),
    ("created_at = 'x'", 'invalid_artifact_record', ArtifactStoreError),
    ("input_fingerprint = 'private canary'", 'invalid_artifact_record', ArtifactStoreError),
])
def test_invalid_rows_fail_closed_without_exposing_stored_data(env, assignment, error, kind):
    artifact = env.publish()
    with closing(sqlite3.connect(env.database)) as connection:
        connection.execute('DROP TRIGGER job_artifacts_immutable')
        connection.execute('PRAGMA ignore_check_constraints=ON')
        connection.execute(f'UPDATE job_artifacts SET {assignment}')
        connection.commit()
    with pytest.raises(kind, match=f'^{error}$') as caught:
        env.service().open_reusable(artifact.artifact_id, env.request())
    assert 'canary' not in str(caught.value)
