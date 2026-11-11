from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Barrier
import pytest
from ayvu.knowledge.glossary.catalog import Entry, GlossaryReference
from ayvu.knowledge.glossary.catalog_store import ManagedGlossaryRepository
from ayvu.preferences.store import JsonPreferencesRepository
from ayvu.projects.profiles.glossary_store import LegacyGlossaryResolver
from ayvu.projects.model import ProjectConflictError
from ayvu.composition.builders.profiles import project_profile_service
from ayvu.projects.profiles.service import ProfileOutcome, ProjectProfileService
from ayvu.projects.profiles.store import JsonProjectProfileRepository, STATE_KEY
from ayvu.projects.profiles.model import (
    ExecutionStage, GlossaryStage, PlanFacts, ProfileError, ProfileOverrides, ProfileReference,
    ProfileRevision, ProjectReference, ProjectTranslationProfile, ProviderRequestProjection,
)
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.kernel import LanguagePair

def plan(target='fr', glossary=True):
    return PlanFacts(ExecutionStage.TRANSLATE, GlossaryStage.POST_CACHE if glossary else GlossaryStage.NONE,
                     ProviderRequestProjection(target, ProfileRevision('a' * 64)))


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'original.epub'
    source.write_bytes(b'original-canary-never-modified')
    processing = tmp_path / 'processing'
    projects = ProjectJsonRepository(processing)
    manifest = projects.create(build_project_request(source, source='en', target='pt'))
    root = tmp_path / 'managed'
    catalog = ManagedGlossaryRepository(root)
    created = catalog.create(GlossaryReference('managed.json'), (Entry('book-canary', 'translate', 'livro'),))
    assert created.failure is None
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'version': 1, 'profiles': {'name-canary': {
        'target_language': 'fr', 'glossary': 'managed.json', 'style': 'literary'}}}))
    prefs = JsonPreferencesRepository(config)
    service = project_profile_service(processing, root)
    project = ProjectReference(manifest.project_id)
    destination = ProfileReference.new()
    return source, projects, root, catalog, config, prefs, service, project, destination


def migrate(local, expected=1, cancel=None):
    _, _, _, _, _, prefs, service, project, destination = local
    return service.migrate(prefs, 'name-canary', project, expected, destination, cancel)


def set_glossary(local, value):
    config = local[4]
    data = json.loads(config.read_text())
    data['profiles']['name-canary']['glossary'] = value
    config.write_text(json.dumps(data))
