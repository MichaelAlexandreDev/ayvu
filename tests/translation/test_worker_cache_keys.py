"""One production per exact key across concurrent document workers."""
from threading import Event, Thread

import pytest

from ayvu.knowledge.cache import CacheLookupState, CacheMissReason, ExactCacheLookup
from ayvu.translation.worker_cache_keys import ArbitratedExactCache


class Store:
    """A dict-backed exact cache; only the states the arbitration reads matter here."""

    def __init__(self):
        self.values, self.lookups, self.puts = {}, [], []

    def lookup(self, key):
        self.lookups.append(key)
        if key in self.values:
            return ExactCacheLookup(CacheLookupState.HIT, value=self.values[key])
        return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)

    def put_if_absent(self, entry):
        key, value = entry
        self.puts.append(key)
        self.values.setdefault(key, value)
        return "stored"


class Worker(Thread):
    def __init__(self, cache, body, check=None):
        super().__init__(daemon=True)
        self.cache, self.body, self.check = cache, body, check
        self.result, self.error = None, None

    def run(self):
        try:
            with self.cache.worker(self.check):
                self.result = self.body(self.cache)
        except BaseException as error:
            self.error = error


def produce(key, value, *, claimed=None, release=None):
    def body(cache):
        if cache.lookup(key).state is CacheLookupState.HIT:
            return "reused"
        if claimed is not None:
            claimed.set()
            assert release.wait(5)
        cache.put_if_absent((key, value))
        return "produced"
    return body


def test_a_repeated_key_is_produced_once_and_the_sibling_reads_it():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    claimed, release = Event(), Event()
    owner = Worker(cache, produce("k", "first", claimed=claimed, release=release))
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, produce("k", "second"))
    sibling.start()
    sibling.join(0.2)
    assert sibling.is_alive()  # Waiting for the owner, not asking the provider.
    release.set()
    owner.join(5), sibling.join(5)
    assert (owner.error, sibling.error) == (None, None)
    assert (owner.result, sibling.result) == ("produced", "reused")
    assert store.puts == ["k"] and store.values == {"k": "first"}


def test_different_keys_never_wait_on_each_other():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    claimed, release = Event(), Event()
    owner = Worker(cache, produce("a", "A", claimed=claimed, release=release))
    owner.start()
    assert claimed.wait(5)
    other = Worker(cache, produce("b", "B"))
    other.start()
    other.join(5)
    assert not other.is_alive() and other.result == "produced"
    release.set()
    owner.join(5)
    assert store.values == {"a": "A", "b": "B"}


def test_owner_failure_wakes_waiters_with_the_same_error():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    claimed, release = Event(), Event()
    failure = RuntimeError("provider_failed")

    def failing(cache):
        assert cache.lookup("k").state is CacheLookupState.MISS
        claimed.set()
        assert release.wait(5)
        raise failure

    owner = Worker(cache, failing)
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, produce("k", "second"))
    sibling.start()
    release.set()
    owner.join(5), sibling.join(5)
    assert owner.error is failure and sibling.error is failure
    assert store.puts == []


def test_an_owner_that_never_writes_releases_at_its_next_lookup():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    claimed, moved_on = Event(), Event()

    def oversize(cache):
        # Too large to cache: the stage skips the write and moves to the next unit.
        assert cache.lookup("big").state is CacheLookupState.MISS
        claimed.set()
        assert moved_on.wait(5)
        cache.lookup("next")
        return "skipped"

    owner = Worker(cache, oversize)
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, produce("big", "own"))
    sibling.start()
    moved_on.set()
    owner.join(5), sibling.join(5)
    assert sibling.error is None and sibling.result == "produced"


def test_crossed_claims_cannot_deadlock():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    both = [Event(), Event()]

    def crossed(mine, theirs, index):
        def body(cache):
            assert cache.lookup(mine).state is CacheLookupState.MISS
            both[index].set()
            assert all(event.wait(5) for event in both)
            # Each moves on without writing, then needs the other's key.
            produce(theirs, f"{theirs}-{index}")(cache)
            return "done"
        return body

    first, second = Worker(cache, crossed("a", "b", 0)), Worker(cache, crossed("b", "a", 1))
    first.start(), second.start()
    first.join(5), second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert (first.error, second.error) == (None, None)


def test_a_waiting_sibling_still_observes_cancellation():
    store = Store()
    cache = ArbitratedExactCache(store, poll_seconds=0.01)
    claimed, release, cancel = Event(), Event(), Event()

    class Cancelled(Exception):
        pass

    def check():
        if cancel.is_set():
            raise Cancelled()

    owner = Worker(cache, produce("k", "first", claimed=claimed, release=release))
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, produce("k", "second"), check=check)
    sibling.start()
    cancel.set()
    sibling.join(5)
    assert isinstance(sibling.error, Cancelled)
    release.set()
    owner.join(5)
    assert owner.result == "produced"


def test_claims_do_not_outlive_their_task():
    cache = ArbitratedExactCache(Store(), poll_seconds=0.01)
    task = Worker(cache, lambda cache: cache.lookup("k").state)
    task.start()
    task.join(5)
    assert task.result is CacheLookupState.MISS
    assert cache._claims == {} and cache._held == {} and cache._checks == {}


def test_delegates_identity_resolution_untouched():
    calls = []

    class Resolver(Store):
        def resolve_pinned(self, reference, scope):
            calls.append(("pinned", reference, scope))
            return "pin"

        def resolve_compatible(self, scope, value_kind, digest, *, limit):
            calls.append(("compatible", scope, value_kind, digest, limit))
            return ()

    cache = ArbitratedExactCache(Resolver())
    assert cache.resolve_pinned("ref", "scope") == "pin"
    assert cache.resolve_compatible("scope", "kind", "digest", limit=3) == ()
    assert calls == [("pinned", "ref", "scope"), ("compatible", "scope", "kind", "digest", 3)]
    with pytest.raises(AttributeError):
        cache.clear


class Wrapping:
    """Like the exact-cache binding: every repository Exception becomes a generic failure."""

    def __init__(self, cache):
        self.cache = cache

    def lookup(self, key):
        try:
            return self.cache.lookup(key)
        except Exception:
            raise RuntimeError("cache_repository_failed") from None


def test_owner_failure_keeps_its_identity_through_an_adapter_that_wraps_exceptions():
    cache = ArbitratedExactCache(Store(), poll_seconds=0.01)
    claimed, release = Event(), Event()
    failure = ValueError("provider_failed")

    def failing(cache):
        assert cache.lookup("k").state is CacheLookupState.MISS
        claimed.set()
        assert release.wait(5)
        raise failure

    owner = Worker(cache, failing)
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, lambda cache: Wrapping(cache).lookup("k"))
    sibling.start()
    release.set()
    owner.join(5), sibling.join(5)
    assert sibling.error is failure


def test_a_check_failure_while_waiting_keeps_its_identity_through_the_adapter():
    cache = ArbitratedExactCache(Store(), poll_seconds=0.01)
    claimed, release = Event(), Event()
    lost = RuntimeError("lease_not_held")

    def check():
        raise lost

    owner = Worker(cache, produce("k", "first", claimed=claimed, release=release))
    owner.start()
    assert claimed.wait(5)
    sibling = Worker(cache, lambda cache: Wrapping(cache).lookup("k"), check=check)
    sibling.start()
    sibling.join(5)
    release.set()
    owner.join(5)
    assert sibling.error is lost and owner.result == "produced"
