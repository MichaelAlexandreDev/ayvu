from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import multiprocessing
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import time

import pytest

from ayvu.project import ProjectConflictError, ProjectError, ProjectSourceError
from ayvu.project_service import resolve_project
from ayvu.project_store import (
    ProjectStoreError,
    ProjectJsonRepository, build_project_request, decode_manifest, encode_manifest,
)


@pytest.fixture
def project_input(tmp_path):
    source = tmp_path / 'source.epub'
    source.write_bytes(b'synthetic-original')
    repository = ProjectJsonRepository(tmp_path / 'processing')
    request = build_project_request(source, source='en', target='pt')
    return source, repository, request


def test_create_reopen_reuse_and_new_language(project_input):
    source, repository, request = project_input
    original = source.read_bytes()
    result = resolve_project(repository, request)
    second = resolve_project(ProjectJsonRepository(repository.root.parent), request)
    assert result.created and not second.created
    assert result.project == second.project == repository.load(result.project.project_id)
    assert result.project.project_id != request.source_sha256
    assert request.source_sha256 == hashlib.sha256(original).hexdigest()
    other = resolve_project(repository, replace(request, target='fr'))
    assert other.created and other.project.project_id != result.project.project_id
    assert source.read_bytes() == original
    if os.name != 'nt':
        assert (repository.root / result.project.project_id).stat().st_mode & 0o777 == 0o700
        assert (repository.root / result.project.project_id / 'project.json').stat().st_mode & 0o777 == 0o600


def test_changed_missing_and_incompatible_source(project_input):
    source, repository, request = project_input
    project = repository.create(request)
    changed_config = repository.resolve(replace(request, configuration_ref='configuration:' + 'a' * 64))
    assert changed_config.created
    assert changed_config.reason == 'new_configuration'
    assert changed_config.project.project_id != project.project_id
    assert repository.load(project.project_id) == project
    source.write_bytes(b'changed')
    changed = build_project_request(source, source='en', target='pt')
    with pytest.raises(ProjectSourceError, match='source_changed'):
        repository.resolve(changed)
    source.unlink()
    with pytest.raises(ProjectSourceError, match='source_missing'):
        repository.resolve(request)
    assert repository.load(project.project_id) == project


def test_concurrent_identical_resolution_is_one_real_project(project_input):
    _, repository, request = project_input
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return ProjectJsonRepository(repository.root.parent).resolve(request)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert sum(result.created for result in results) == 1
    assert len({result.project.project_id for result in results}) == 1
    assert len(repository.list_projects()) == 1


def test_concurrent_expected_revision_writers_have_one_winner(project_input):
    _, repository, request = project_input
    project = repository.create(request)
    barrier = Barrier(2)
    def run(index):
        barrier.wait(timeout=5)
        try:
            return repository.save(replace(project, locations=(f'reports-{index}',)), expected_revision=1)
        except ProjectConflictError:
            return None
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(run, range(2)))
    assert sum(result is not None for result in results) == 1
    saved = repository.load(project.project_id)
    assert saved.revision == 2
    assert saved in results
    assert saved.created_at == project.created_at


@pytest.mark.parametrize('failure', ['replace', 'fsync'])
def test_failed_update_preserves_prior_bytes(project_input, monkeypatch, failure):
    _, repository, request = project_input
    project = repository.create(request)
    path = repository.root / project.project_id / 'project.json'
    before = path.read_bytes()
    def fail(*args):
        raise OSError('secret-canary-path')
    monkeypatch.setattr('ayvu.project_store.os.' + failure, fail)
    with pytest.raises(ProjectError) as caught:
        repository.save(replace(project, locations=('reports',)), expected_revision=1)
    assert 'secret-canary' not in str(caught.value)
    assert path.read_bytes() == before
    assert not list(path.parent.glob('.project-*'))


def test_ambiguous_project_never_picks_arbitrary_match(project_input):
    _, repository, request = project_input
    project = repository.create(request)
    duplicate = replace(project, project_id=str(uuid4()))
    directory = repository.root / duplicate.project_id
    directory.mkdir()
    (directory / 'project.json').write_bytes(encode_manifest(duplicate))
    with pytest.raises(ProjectConflictError, match='ambiguous_project'):
        repository.resolve(request)


@pytest.mark.parametrize('kind', ['source_link', 'source_parent_link', 'workspace_link', 'manifest_link', 'location_link', 'hardlink'])
def test_symlinks_aliases_and_escape_fail_closed(project_input, tmp_path, kind):
    source, repository, request = project_input
    outside = tmp_path / 'outside'
    outside.mkdir()
    project = repository.create(request)
    directory = repository.root / project.project_id
    if kind == 'source_link':
        link = tmp_path / 'link.epub'
        link.symlink_to(source)
        operation = lambda: build_project_request(link, source='en', target='pt')
    elif kind == 'source_parent_link':
        link = tmp_path / 'linked-parent'
        link.symlink_to(tmp_path, target_is_directory=True)
        operation = lambda: build_project_request(link / source.name, source='en', target='pt')
    elif kind == 'workspace_link':
        other = tmp_path / 'linked-processing'
        other.symlink_to(repository.root.parent, target_is_directory=True)
        operation = lambda: ProjectJsonRepository(other).resolve(request)
    elif kind == 'manifest_link':
        path = directory / 'project.json'
        path.unlink()
        path.symlink_to(source)
        operation = lambda: repository.load(project.project_id)
    elif kind == 'location_link':
        (directory / 'reports').symlink_to(outside, target_is_directory=True)
        operation = lambda: repository.save(replace(project, locations=('reports/x',)), expected_revision=1)
    else:
        os.link(source, directory / 'copy')
        operation = lambda: build_project_request(source, source='en', target='pt')
    with pytest.raises(ProjectError):
        operation()
    assert source.read_bytes() == b'synthetic-original'
    assert not list(outside.iterdir())


@pytest.mark.parametrize('payload', [b'{bad', b'[]', b'{' * 40000])
def test_corruption_does_not_mutate_source_or_other_state(project_input, payload):
    source, repository, request = project_input
    project = repository.create(request)
    path = repository.root / project.project_id / 'project.json'
    path.write_bytes(payload)
    with pytest.raises(ProjectError):
        repository.resolve(request)
    assert path.read_bytes() == payload
    assert source.read_bytes() == b'synthetic-original'


def test_config_and_glossary_changes_have_distinct_fingerprints(project_input, tmp_path):
    source, _, _ = project_input
    glossary = tmp_path / 'terms.json'
    glossary.write_text('{"test":"one"}')
    first = build_project_request(source, source='en', target='pt', glossary_path=glossary,
                                  selections={'url': 'http://localhost'})
    glossary.write_text('{"test":"two"}')
    second = build_project_request(source, source='en', target='pt', glossary_path=glossary,
                                   selections={'url': 'http://localhost'})
    assert first.configuration_ref != second.configuration_ref
    project = ProjectJsonRepository(tmp_path / 'another').create(first)
    assert 'secret-canary' not in repr(first)
    assert b'secret-canary' not in encode_manifest(project)


def test_incomplete_creation_is_explicit_recoverable_error(project_input):
    source, repository, request = project_input
    repository.root.mkdir(parents=True)
    (repository.root / '.staging-interrupted').mkdir()
    with pytest.raises(ProjectError, match='incomplete_project_creation'):
        repository.resolve(request)
    assert source.read_bytes() == b'synthetic-original'
    # Recovery is an explicit preservation/move decision, never automatic GC.
    (repository.root / '.staging-interrupted').rename(repository.root.parent / 'preserved-stage')
    assert repository.resolve(request).created


def _resolve_in_process(processing, source, barrier, queue):
    request = build_project_request(Path(source), source='en', target='pt')
    barrier.wait(timeout=10)
    result = ProjectJsonRepository(Path(processing)).resolve(request)
    queue.put((result.project.project_id, result.created))


def test_independent_processes_resolve_one_identity(project_input):
    source, repository, _ = project_input
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(3)
    queue = context.Queue()
    processes = [context.Process(target=_resolve_in_process, args=(
        str(repository.root.parent), str(source), barrier, queue)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert len({result[0] for result in results}) == 1
        assert sum(result[1] for result in results) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


@pytest.mark.parametrize('failure', ['replace', 'rename'])
def test_creation_failure_cleans_own_stage_and_retry_succeeds(project_input, monkeypatch, failure):
    source, repository, request = project_input
    with monkeypatch.context() as context:
        def fail(*args):
            raise OSError('synthetic-disk-failure')
        context.setattr('ayvu.project_store.os.' + failure, fail)
        with pytest.raises(ProjectError):
            repository.resolve(request)
    assert not list(repository.root.glob('.staging-*'))
    assert not list(repository.root.glob('*/project.json'))
    assert repository.resolve(request).created
    assert source.read_bytes() == b'synthetic-original'


def test_directory_sync_after_publication_is_uncertain_but_retry_reuses(project_input, monkeypatch):
    _, repository, request = project_input
    with monkeypatch.context() as context:
        original = repository._sync_directory
        def fail_only_root(directory):
            if directory == repository.root:
                raise OSError('synthetic-durability-failure')
            original(directory)
        context.setattr(repository, '_sync_directory', fail_only_root)
        with pytest.raises(ProjectError, match='project_commit_uncertain'):
            repository.resolve(request)
    projects = repository.list_projects()
    assert len(projects) == 1
    retry = repository.resolve(request)
    assert not retry.created
    assert retry.project == projects[0]


def test_post_replace_sync_error_leaves_complete_revision_for_reload(project_input, monkeypatch):
    _, repository, request = project_input
    before = repository.create(request)
    with monkeypatch.context() as context:
        def fail(*args):
            raise OSError('synthetic-durability-failure')
        context.setattr(repository, '_sync_directory', fail)
        with pytest.raises(ProjectError, match='project_commit_uncertain'):
            repository.save(replace(before, locations=('reports',)), expected_revision=1)
    after = repository.load(before.project_id)
    assert after.revision == 2
    assert after.locations == ('reports',)


def test_location_collision_casefold_and_source_inside_workspace_are_rejected(project_input):
    _, repository, request = project_input
    project = repository.create(request)
    with pytest.raises(ProjectError):
        replace(project, locations=('Reports/x', 'reports/x'))
    with pytest.raises(ProjectSourceError, match='source_inside_workspace'):
        inside = repository.root / project.project_id / 'project.json'
        repository.resolve(build_project_request(inside, source='en', target='pt'))


@pytest.mark.parametrize('endpoint', [
    'http://user:secret-canary@localhost', 'http://localhost/?api_key=secret-canary',
    'http://localhost/#secret-canary',
])
def test_credential_bearing_endpoint_is_rejected_before_identity(project_input, endpoint):
    source, repository, _ = project_input
    with pytest.raises(ProjectError) as caught:
        build_project_request(source, source='en', target='pt', selections={'url': endpoint})
    assert 'secret-canary' not in str(caught.value)
    assert not repository.root.exists()


def test_existing_permissive_workspace_is_restricted(project_input):
    _, repository, request = project_input
    repository.root.mkdir(parents=True, mode=0o755)
    repository.resolve(request)
    if os.name != 'nt':
        assert repository.root.stat().st_mode & 0o777 == 0o700


def test_explicit_glossary_symlink_retains_legacy_compatibility(project_input, tmp_path):
    source, _, _ = project_input
    glossary = tmp_path / 'terms.json'
    glossary.write_text('{}')
    link = tmp_path / 'terms-link.json'
    link.symlink_to(glossary)
    first = build_project_request(source, source='en', target='pt', glossary_path=glossary)
    second = build_project_request(source, source='en', target='pt', glossary_path=link)
    assert first == second


def test_project_resolves_database_locations_without_changing_manifest(project_input):
    source, repository, request = project_input
    project = repository.create(request)
    manifest = repository.root / project.project_id / 'project.json'
    before = manifest.read_bytes()
    paths = repository.resolve_database_paths(project.project_id)
    assert paths.database == manifest.parent / 'project.sqlite'
    assert paths.backups == manifest.parent / 'backups' / 'schema'
    assert not paths.database.exists() and not paths.backups.exists()
    assert manifest.read_bytes() == before
    assert str(repository.root) not in repr(paths)


@pytest.mark.parametrize('part', ['database', 'backups'])
def test_resolved_database_paths_reject_links(project_input, tmp_path, part):
    _, repository, request = project_input
    project = repository.create(request)
    directory = repository.root / project.project_id
    if part == 'database':
        outside = tmp_path / 'outside.sqlite'
        outside.write_bytes(b'preserve')
        (directory / 'project.sqlite').symlink_to(outside)
    else:
        outside = tmp_path / 'outside'
        outside.mkdir()
        (directory / 'backups').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectError):
        repository.resolve_database_paths(project.project_id)


def test_database_resolver_requires_recovery_for_interrupted_publication(project_input):
    _, repository, request = project_input
    project = repository.create(request)
    directory = repository.root / project.project_id
    staged = directory / '.migration-synthetic.sqlite'
    staged.write_bytes(b'preserve-complete-database')
    database = directory / 'project.sqlite'
    os.link(staged, database)
    with pytest.raises(ProjectError, match='^project_database_recovery_required$'):
        repository.resolve_database_paths(project.project_id)
    assert staged.samefile(database) and database.stat().st_nlink == 2
    assert database.read_bytes() == b'preserve-complete-database'
    staged.unlink()
    assert repository.resolve_database_paths(project.project_id).database == database


def test_artifact_root_is_validated_without_creating_anything(project_input):
    _, repository, request = project_input
    project = repository.create(request)
    manifest = repository._manifest_path(project.project_id)
    before = manifest.read_bytes()
    root = repository.resolve_artifact_root(project.project_id)
    assert root == manifest.parent / 'artifacts'
    assert not root.exists() and manifest.read_bytes() == before
    root.mkdir()
    assert repository.resolve_artifact_root(project.project_id) == root


def test_artifact_root_rejects_a_file_a_symlink_and_an_unknown_project(project_input, tmp_path):
    _, repository, request = project_input
    project = repository.create(request)
    root = repository._manifest_path(project.project_id).parent / 'artifacts'
    root.write_bytes(b'not a directory')
    with pytest.raises(ProjectError, match='^invalid_project_artifact_directory$'):
        repository.resolve_artifact_root(project.project_id)
    root.unlink()
    outside = tmp_path / 'outside'
    outside.mkdir()
    root.symlink_to(outside)
    with pytest.raises(ProjectError, match='^symlink_path$'):
        repository.resolve_artifact_root(project.project_id)
    with pytest.raises(ProjectError):
        repository.resolve_artifact_root(str(uuid4()))


def _guard(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    return repository, project, repository.run_guard(project.project_id)


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
