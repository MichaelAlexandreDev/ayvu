from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier


from ayvu.knowledge.glossary.catalog_store import ManagedGlossaryRepository
from ayvu.preferences.store import JsonPreferencesRepository
from ayvu.projects.profiles.glossary_store import LegacyGlossaryResolver
from ayvu.projects.profiles.service import ProfileOutcome, ProjectProfileService
from ayvu.projects.profiles.store import JsonProjectProfileRepository
from ayvu.projects.profiles.model import ProjectTranslationProfile
from ayvu.projects.store import ProjectJsonRepository

from tests.projects.profiles.project_profile_integration_support import local, migrate


def test_concurrent_divergent_expected_revision_updates_have_one_winner(local):
    barrier = Barrier(2)
    processing, root, project, destination = local[1].root.parent, local[2], local[7], local[8]
    class RacingRepository(JsonProjectProfileRepository):
        def read(self, project):
            value = super().read(project)
            barrier.wait(timeout=10)
            return value
    def write(target):
        repository = RacingRepository(ProjectJsonRepository(processing))
        service = ProjectProfileService(repository, LegacyGlossaryResolver(ManagedGlossaryRepository(root)))
        return service.write(project, 1, destination, ProjectTranslationProfile(target))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ('de', 'es')))
    assert sorted(r.outcome.value for r in results) == ['conflict', 'created']
    state = local[6].repository.read(project)
    winner = next(r for r in results if r.outcome is ProfileOutcome.CREATED)
    assert state == winner.state and len(state.records) == 1
    assert local[1].load(project.value).revision == 2


def test_divergent_migrations_cannot_both_publish_from_one_project_revision(local):
    first = migrate(local)
    data = json.loads(local[4].read_text())
    data['profiles']['name-canary']['style'] = 'technical'
    local[4].write_text(json.dumps(data))
    second = migrate(local)
    assert first.outcome is ProfileOutcome.MIGRATED and second.outcome is ProfileOutcome.CONFLICT
    assert local[6].repository.read(local[7]) == first.state


def test_concurrent_divergent_migrations_same_destination_do_not_overwrite(local, tmp_path):
    barrier = Barrier(2)
    other_config = tmp_path / 'other-config.json'
    other = json.loads(local[4].read_text())
    other['profiles']['name-canary']['target_language'] = 'de'
    other_config.write_text(json.dumps(other))
    class RacingRepository(JsonProjectProfileRepository):
        def read(self, project):
            state = super().read(project)
            barrier.wait(timeout=10)
            return state
    def run(config):
        service = ProjectProfileService(RacingRepository(ProjectJsonRepository(local[1].root.parent)),
            LegacyGlossaryResolver(ManagedGlossaryRepository(local[2])))
        return service.migrate(JsonPreferencesRepository(config), 'name-canary', local[7], 1, local[8])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (local[4], other_config)))
    assert sorted(r.outcome.value for r in results) == ['conflict', 'migrated']
    state = local[6].repository.read(local[7])
    assert len(state.records) == len(state.migrations) == 1
    assert state == next(r.state for r in results if r.outcome is ProfileOutcome.MIGRATED)
