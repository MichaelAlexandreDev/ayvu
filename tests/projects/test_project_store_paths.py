import os
from uuid import uuid4


import pytest

from ayvu.projects.model import ProjectError

from tests.projects.project_store_support import project_input


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
