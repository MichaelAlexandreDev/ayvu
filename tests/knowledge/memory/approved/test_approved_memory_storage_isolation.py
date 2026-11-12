

from ayvu.knowledge.memory.approved.model import MemoryState
from ayvu.knowledge.memory.approved.store import MEMORY_MIGRATIONS
from ayvu.v1.cache import CacheKey, TranslationCache
from tests.support.approved_memory_fixtures import PAIR, PROJECT_SCOPE, SCOPES

from tests.knowledge.memory.approved.approved_memory_support import env


def test_operational_cache_legacy_rows_and_governed_store_are_isolated(env):
    cache_path = env.root / 'cache.sqlite'
    key = CacheKey('Café  Hello', PAIR)
    with TranslationCache(cache_path) as cache:
        cache.set(key, 'legacy')
        cache.connection.execute("UPDATE translations SET created_at = '2000-01-01'")
        cache.connection.commit()
    before = cache_path.read_bytes()
    repository = env.open()
    assert repository.list_entries(scopes=SCOPES).revisions == ()
    assert cache_path.read_bytes() == before
    approved = env.approve(env.create())
    with TranslationCache(cache_path) as cache:
        cache.delete_entries(before='2001-01-01')
        assert cache.get(key) is None
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    cache_path.unlink()
    with TranslationCache(cache_path) as cache:
        cache.set(key, 'new operational value')
    env.repo.review(env.review(approved, MemoryState.DEPRECATED))
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == 'new operational value'
        cache_tables = {row[0] for row in cache.connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert 'translations' in cache_tables and not any(name.startswith('tm_') for name in cache_tables)
    memory_tables = {row[0] for row in env.raw("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert 'translations' not in memory_tables
    env.database.unlink()
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == 'new operational value'


def test_v2_cache_values_namespaces_clear_and_replacement_stay_independent(env):
    from ayvu.knowledge.cache.repository import SQLiteExactCache
    from ayvu.knowledge.cache.schema import participating_connection
    from tests.support.cache_repository_v2_support import entry
    path = env.root / 'cache-v2.sqlite'
    cache = SQLiteExactCache(path)
    item = entry(source='Café  Hello', value='Olá')
    cache.put_if_absent(item)
    assert env.open().list_entries(scopes=SCOPES).revisions == ()
    approved = env.approve(env.create())
    before = env.database.read_bytes()
    assert item.key.value_kind.value != approved.value.target.kind.value
    with participating_connection(path) as db:
        db.execute('DELETE FROM cache_entries_v2')
        db.commit()
    assert env.database.read_bytes() == before
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE,)) == approved
    path.unlink()
    cache = SQLiteExactCache(path)
    cache.put_if_absent(item)
    env.repo.review(env.review(approved, MemoryState.DEPRECATED))
    assert cache.lookup(item.key).value == 'Olá'


def test_snapshot_schema_history_is_frozen():
    assert [step.version for step in MEMORY_MIGRATIONS] == [1, 2, 3]
    assert MEMORY_MIGRATIONS[1].checksum == 'aacfd30a61a1718759aa821466bec626d6bae62c17c172c4e395acc4bbfb6243'
