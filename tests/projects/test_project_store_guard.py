
import time

import pytest

from ayvu.projects.store import ProjectStoreError

from tests.projects.project_store_support import _guard, project_input


def test_run_guard_is_held_per_plan_and_released_at_the_end(project_input):
    _, _, guard = _guard(project_input)
    with guard.exclusive('sha256:one') as first:
        assert first is True
        started = time.monotonic()
        with guard.exclusive('sha256:one') as again, guard.exclusive('sha256:two') as other:
            assert again is False and other is True
        assert time.monotonic() - started < 2  # A refusal does not wait for the other run.
    with guard.exclusive('sha256:one') as later:
        assert later is True


def test_run_guard_releases_when_the_holder_raises(project_input):
    _, _, guard = _guard(project_input)
    with pytest.raises(KeyboardInterrupt):
        with guard.exclusive('sha256:one'):
            raise KeyboardInterrupt
    with guard.exclusive('sha256:one') as later:
        assert later is True


def test_run_guard_does_not_swallow_a_busy_error_from_inside(project_input):
    _, _, guard = _guard(project_input)
    with pytest.raises(ProjectStoreError, match='workspace_busy'):
        with guard.exclusive('sha256:one'):
            raise ProjectStoreError('workspace_busy')
    with pytest.raises(OSError):
        with guard.exclusive('sha256:one'):
            raise OSError('inside')


def test_run_guard_names_its_file_with_no_trace_of_the_plan(project_input):
    repository, project, guard = _guard(project_input)
    with guard.exclusive('sha256:one'):
        pass
    root = repository.resolve_artifact_root(project.project_id).parent / 'runs'
    assert [len(path.stem) for path in root.iterdir()] == [64]
    assert 'one' not in next(root.iterdir()).name


def test_run_guard_refuses_a_link_or_a_blocked_folder_with_a_bounded_code(project_input, tmp_path):
    repository, project, guard = _guard(project_input)
    root = repository.resolve_artifact_root(project.project_id).parent / 'runs'
    root.write_bytes(b'blocked')
    with pytest.raises(ProjectStoreError) as blocked:
        with guard.exclusive('sha256:one'):
            pass
    assert str(blocked.value) == 'run_guard_unavailable'
    root.unlink()
    root.mkdir()
    (tmp_path / 'target').write_bytes(b'')
    with guard.exclusive('sha256:one'):
        pass
    lock = next(root.iterdir())
    lock.unlink()
    lock.symlink_to(tmp_path / 'target')
    with pytest.raises(ProjectStoreError) as linked:
        with guard.exclusive('sha256:one'):
            pass
    assert str(linked.value) == 'symlink_path'


def test_read_paths_and_guard_match_the_writing_ones_and_create_nothing(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    assert repository.read_database_paths(project.project_id) == repository.resolve_database_paths(project.project_id)
    guard = repository.read_run_guard(project.project_id)
    assert not guard.held('sha256:' + 'a' * 64)
    assert not (repository.root / project.project_id / 'runs').exists()


def test_reading_paths_and_the_guard_does_not_wait_for_a_writer(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    with repository._transaction():
        started = time.monotonic()
        repository.read_database_paths(project.project_id)
        repository.read_run_guard(project.project_id)
        assert time.monotonic() - started < 1


def test_held_sees_a_live_holder_and_writes_nothing(project_input):
    repository, project, guard = _guard(project_input)
    fingerprint = 'sha256:' + 'a' * 64
    with guard.exclusive(fingerprint) as alone:
        assert alone
        lock = next((repository.root / project.project_id / 'runs').iterdir())
        before = lock.stat().st_mtime_ns, lock.read_bytes()
        assert guard.held(fingerprint)
        assert not guard.held('sha256:' + 'b' * 64)
    assert not guard.held(fingerprint)
    assert (lock.stat().st_mtime_ns, lock.read_bytes()) == before


def test_held_refuses_a_link_with_a_bounded_code(project_input, tmp_path):
    repository, project, guard = _guard(project_input)
    fingerprint = 'sha256:' + 'a' * 64
    runs = repository.root / project.project_id / 'runs'
    runs.mkdir()
    (tmp_path / 'elsewhere').write_text('x')
    guard._path(fingerprint).symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ProjectStoreError) as refused:
        guard.held(fingerprint)
    assert str(refused.value) in ('symlink_path', 'run_guard_unavailable')
