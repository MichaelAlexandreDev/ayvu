"""Local adapters, real v1 input and project CAS. No user directories or network."""
from dataclasses import replace
import json
import os

import pytest

from ayvu.knowledge.glossary.catalog import Entry, GlossaryReference
from ayvu.projects.model import ProjectConflictError
from ayvu.composition.builders.profiles import project_profile_service
from ayvu.projects.profiles.service import ProfileOutcome
from ayvu.projects.profiles.store import STATE_KEY
from ayvu.projects.profiles.model import ProfileOverrides

from tests.projects.profiles.project_profile_integration_support import local, migrate, plan, set_glossary


def test_production_preferences_exact_binding_atomic_update_and_restart(local):
    source, projects, root, catalog, config, prefs, service, project, destination = local
    before_source, before_config = source.read_bytes(), config.read_bytes()
    original = projects.load(project.value)
    glossary = catalog.read(GlossaryReference('managed.json')).value
    migrated = migrate(local)
    assert migrated.outcome is ProfileOutcome.MIGRATED
    profile = migrated.state.profile(destination)
    assert profile.glossary.reference == glossary.reference
    assert profile.glossary.revision.value == glossary.revision
    manifest = projects.load(project.value)
    assert manifest.revision == original.revision + 1
    assert manifest.request() == original.request()
    assert STATE_KEY in dict(manifest.metadata)
    assert 'name-canary' not in json.dumps(migrated.state.to_mapping())
    assert str(root) not in json.dumps(migrated.state.to_mapping())
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    assert resolved.outcome is ProfileOutcome.RESOLVED
    assert resolved.snapshot.project_revision == 2
    restarted = project_profile_service(projects.root.parent, root)
    loaded = restarted.repository.read(project)
    assert loaded.snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert loaded.raw_projection(resolved.snapshot.raw_semantic_contribution()['snapshot_reference']) == resolved.snapshot.effects.raw_projection()
    assert restarted.resolve(project, 2, destination, profile.revision, plan()).snapshot == resolved.snapshot
    assert migrate(local).outcome is ProfileOutcome.ALREADY_MIGRATED
    updated = restarted.write(project, 3, destination, replace(profile, style='technical'), profile.revision)
    assert updated.outcome is ProfileOutcome.UPDATED
    assert updated.state.snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert migrate(local).outcome is ProfileOutcome.ALREADY_MIGRATED
    assert restarted.repository.read(project).profile(destination).style == 'technical'
    assert source.read_bytes() == before_source and config.read_bytes() == before_config
    storage = projects._manifest_path(project.value).parent
    assert not list(storage.glob('*.sqlite*'))
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (storage / 'profiles').glob('*.json'))
    assert (storage / 'profiles').stat().st_mode & 0o777 == 0o700


def test_legacy_absolute_path_inside_managed_catalog_is_compatible(local):
    set_glossary(local, str(local[2] / 'managed.json'))
    result = migrate(local)
    assert result.outcome is ProfileOutcome.MIGRATED
    assert result.state.profile(local[8]).glossary.reference == GlossaryReference('managed.json')
    assert str(local[2]) not in json.dumps(result.state.to_mapping())


@pytest.mark.parametrize('kind,reason', [
    ('missing', 'missing'), ('absolute_external', 'external'), ('relative_external', 'external'),
    ('case_alias', 'ambiguous'), ('unicode_alias', 'ambiguous'), ('symlink', 'unsafe'),
    ('parent_symlink', 'unsafe'), ('hardlink', 'unsafe'), ('directory', 'unsafe'), ('invalid', 'unsafe'),
])
def test_legacy_path_rejections_use_catalog_rules_without_partial_project_update(local, tmp_path, kind, reason):
    root, projects, project = local[2], local[1], local[7]
    selected = root / 'managed.json'
    value = 'managed.json'
    if kind == 'missing':
        value = 'missing.json'
    elif kind in ('absolute_external', 'relative_external'):
        external = tmp_path / 'outside.json'
        external.write_text('{}')
        value = str(external) if kind == 'absolute_external' else '../outside.json'
    elif kind == 'case_alias':
        (root / 'MANAGED.json').write_text('{}')
    elif kind == 'unicode_alias':
        value = 'caf\u00e9.json'
        (root / value).write_text('{}')
        (root / 'cafe\u0301.json').write_text('{}')
    elif kind == 'symlink':
        selected.unlink()
        selected.symlink_to(tmp_path / 'outside.json')
    elif kind == 'parent_symlink':
        (tmp_path / 'alias').symlink_to(root, target_is_directory=True)
        value = str(tmp_path / 'alias' / 'managed.json')
    elif kind == 'hardlink':
        os.link(selected, tmp_path / 'hardlink.json')
    elif kind == 'directory':
        selected.unlink()
        selected.mkdir()
    else:
        selected.write_text('[]')
    set_glossary(local, value)
    before = projects.load(project.value)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY
    assert result.glossary_failure == reason
    assert result.state is None and result.snapshot is None
    assert projects.load(project.value) == before
    assert local[6].repository.read(project).records == ()


def test_glossary_change_after_resolution_is_refused_by_exact_revision_guard(local, monkeypatch):
    service = local[6]
    original = service.glossaries.resolve_legacy
    def race(path, cancel=None):
        binding = original(path, cancel)
        changed = local[3].update(binding.reference, (Entry('new', 'preserve'),), binding.revision.value)
        assert changed.failure is None
        return binding
    monkeypatch.setattr(service.glossaries, 'resolve_legacy', race)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY
    assert result.glossary_failure == 'stale'
    assert local[1].load(local[7].value).revision == 1
    assert service.repository.read(local[7]).records == ()


def test_glossary_change_immediately_before_manifest_publication_is_revalidated(local, monkeypatch):
    service = local[6]
    original = service.repository._publish_manifest
    def race(path, manifest, cancel, validate):
        # An uncooperative writer bypasses #157's lock. Last-moment validation
        # still detects a changed revision before publishing the project pointer.
        (local[2] / 'managed.json').write_text('{}')
        return original(path, manifest, cancel, validate)
    monkeypatch.setattr(service.repository, '_publish_manifest', race)
    result = migrate(local)
    assert result.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and result.glossary_failure == 'stale'
    assert service.repository.read(local[7]).records == ()
    assert local[1].load(local[7].value).revision == 1


def test_stale_glossary_prevents_snapshot_publication_but_keeps_historical_snapshot(local):
    migrated = migrate(local)
    service, project, destination = local[6:]
    profile = migrated.state.profile(destination)
    resolved = service.resolve(project, 2, destination, profile.revision, plan())
    current = local[3].read(profile.glossary.reference).value
    assert local[3].update(current.reference, (Entry('new', 'preserve'),), current.revision).failure is None
    failed = service.resolve(project, 3, destination, profile.revision, plan('de'), ProfileOverrides('de'))
    assert failed.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and failed.glossary_failure == 'stale'
    assert failed.snapshot is None
    assert service.repository.read(project).snapshot(resolved.snapshot.reference) == resolved.snapshot
    assert local[1].load(project.value).revision == 3
    retry = service.resolve(project, 2, destination, profile.revision, plan())
    assert retry.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and retry.glossary_failure == 'stale'
    unchanged = service.write(project, 3, destination, profile, profile.revision)
    assert unchanged.outcome is ProfileOutcome.UNRESOLVED_GLOSSARY and unchanged.glossary_failure == 'stale'


def test_unrelated_project_update_and_stale_manifest_conflict(local):
    service, project, destination = local[6:]
    manifest = local[1].load(project.value)
    local[1].save(replace(manifest, locations=('artifacts',)), expected_revision=1)
    assert migrate(local).outcome is ProfileOutcome.CONFLICT
    assert service.repository.read(project).records == ()
    migrated = migrate(local, expected=2)
    assert migrated.outcome is ProfileOutcome.MIGRATED
    current = local[1].load(project.value)
    local[1].save(replace(current, locations=('artifacts', 'reports')), expected_revision=3)
    state = service.repository.read(project)
    assert state.project_revision == 4 and state.profile(destination).style == 'literary'
    with pytest.raises(ProjectConflictError, match='revision_conflict'):
        local[1].save(current, expected_revision=3)


@pytest.mark.parametrize('phase', ['before', 'prepared', 'write', 'pre_sync', 'post_sync'])
def test_cancellation_and_persistence_failures_preserve_prior_bytes_and_report_no_success(local, monkeypatch, phase):
    service, project, destination = local[6:]
    projects = service.repository.projects
    manifest_path = projects._manifest_path(project.value)
    # Existing permissions must survive replacement and rollback.
    manifest_path.chmod(0o400)
    before = manifest_path.read_bytes()
    hits = []
    cancel = None
    if phase == 'before':
        def cancel():
            hits.append('before')
            return True
    elif phase == 'prepared':
        prepared = False
        original = service.repository._temporary
        def temporary(path, data, mode=0o600):
            nonlocal prepared
            value = original(path, data, mode)
            if path == manifest_path:
                prepared = True
            return value
        monkeypatch.setattr(service.repository, '_temporary', temporary)
        def cancel():
            if prepared:
                hits.append('prepared')
            return prepared
    elif phase == 'write':
        def fail(*args):
            hits.append('write')
            raise OSError('private-path-credential-canary')
        monkeypatch.setattr(service.repository, '_temporary', fail)
    else:
        original = projects._sync_directory
        count = 0
        def sync(directory):
            nonlocal count
            if directory == manifest_path.parent:
                count += 1
                if count == (1 if phase == 'pre_sync' else 2):
                    hits.append(phase)
                    raise OSError('private-path-credential-canary')
            return original(directory)
        monkeypatch.setattr(projects, '_sync_directory', sync)
    result = migrate(local, cancel=cancel)
    assert hits == [phase]
    assert result.outcome is (ProfileOutcome.CANCELLED if phase in ('before', 'prepared') else ProfileOutcome.PERSISTENCE)
    assert result.state is None and result.snapshot is None
    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_mode & 0o777 == 0o400
    assert service.repository.read(project).records == ()
    assert 'canary' not in str(result.diagnostic())
