"""These tests deliberately exercise real safe bootstrap, not the schema fixture."""
from contextlib import closing
import sqlite3

from ayvu.approved_memory import MemoryScope, MemoryState, ScopeKind
from ayvu.approved_memory_store import MEMORY_MIGRATIONS, memory_migration_runner, open_approved_memory
from ayvu.cache import CacheKey, TranslationCache
from approved_memory_fixtures import MemoryEnvironment, PAIR, PROJECT_SCOPE, SCOPES


def test_real_bootstrap_create_reopen_and_query_without_cache(tmp_path):
    env = MemoryEnvironment(tmp_path, migrate=True)
    candidate = env.create()
    approved = env.approve(candidate)
    assert env.open(migrate=True).get(approved.revision_id, scopes=(PROJECT_SCOPE,)) == approved
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)).state is MemoryState.APPROVED
    assert not (tmp_path / 'cache.sqlite').exists()
    assert memory_migration_runner().migrate(env.database, tmp_path / 'backups' / 'approved-memory-schema').status == 'unchanged'
    assert env.database.stat().st_mode & 0o777 == 0o600


def test_real_upgrade_preserves_independent_history_and_verified_backup(tmp_path):
    database = tmp_path / 'approved-memory.sqlite'
    backups = tmp_path / 'backups' / 'approved-memory-schema'
    runner = memory_migration_runner()
    first = runner.migrate(database, backups, target_version=1)
    assert first.ok and first.to_version == 1
    result = runner.migrate(database, backups)
    assert result.ok and result.to_version == 2 and result.applied == (2,) and result.backup_verified
    with closing(sqlite3.connect(backups / result.backup_name)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'tm_%'").fetchall() == []
    with closing(runner.connect(database, readonly=True)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT version, checksum FROM ayvu_schema_migrations ORDER BY version').fetchall() == [
            (step.version, step.checksum) for step in MEMORY_MIGRATIONS]
    assert runner.migrate(database, backups).status == 'unchanged'


def test_production_initialization_never_promotes_representative_legacy_rows(tmp_path):
    path = tmp_path / 'cache.sqlite'
    with TranslationCache(path) as cache:
        cache.set(CacheKey('same source', PAIR), 'same target')
        cache.set(CacheKey('provider __AYVU_TAG_0__', PAIR), 'provider __AYVU_TAG_0__')
        cache.set(CacheKey('outdated', PAIR), 'outdated target')
    before = path.read_bytes()
    repo = open_approved_memory(tmp_path, allowed_scopes=frozenset(SCOPES), provenance={})
    assert repo.list_entries(scopes=SCOPES).revisions == ()
    assert path.read_bytes() == before
    with closing(sqlite3.connect(tmp_path / 'approved-memory.sqlite')) as db:
        assert db.execute('SELECT COUNT(*) FROM tm_revisions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM tm_events').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_schema WHERE name IN ('translations', 'cache_entries_v2')").fetchall() == []


def test_real_projects_directory_resolution_preserves_original_manifest_and_project_database(tmp_path):
    from ayvu.project_store import ProjectJsonRepository, build_project_request
    from ayvu.project_schema import open_provenance_repository
    original = tmp_path / 'original.epub'
    original.write_bytes(b'synthetic original')
    projects = ProjectJsonRepository(tmp_path / 'processing')
    project = projects.create(build_project_request(original, source='en', target='pt-br'))
    paths = projects.resolve_database_paths(project.project_id)
    provenance = open_provenance_repository(paths.database, paths.backups, project.project_id)
    manifest = paths.database.with_name('project.json')
    before = manifest.read_bytes(), paths.database.read_bytes(), original.read_bytes()
    scope = MemoryScope(ScopeKind.PROJECT, project.project_id)
    repo = open_approved_memory(paths.database.parent, allowed_scopes=frozenset({scope}),
                                provenance={project.project_id: provenance})
    assert repo.list_entries(scopes=(scope,)).revisions == ()
    assert (manifest.read_bytes(), paths.database.read_bytes(), original.read_bytes()) == before
    assert paths.database.with_name('approved-memory.sqlite').is_file()
