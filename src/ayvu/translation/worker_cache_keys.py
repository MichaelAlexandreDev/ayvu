"""One provider production per exact key while documents run concurrently.

A worker that misses a key claims it and produces the value; a sibling that
misses the same key waits for that production and then reads the stored value
instead of asking the provider again. A thread holds at most one claim, released
by its next lookup, its write or the end of its document, and it never waits
while holding a claim, so two workers cannot wait on each other. If the owner
fails, every waiter raises that same error, so the book stops with one cause.
Another process may still write the same key: ``put_if_absent`` keeps its
idempotent same-value and ``cache_write_conflict`` different-value rules.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Lock, get_ident

from ..knowledge.cache import CacheLookupState


class _Claim:
    def __init__(self) -> None:
        self.done = Event()
        self.error: BaseException | None = None


class ArbitratedExactCache:
    """Wraps the shared exact-cache adapter; each call still opens its own connection."""

    def __init__(self, cache, *, poll_seconds: float = 0.05) -> None:
        self._cache, self._poll = cache, poll_seconds
        self._lock = Lock()
        self._claims: dict[object, _Claim] = {}
        self._held: dict[int, object] = {}
        self._checks: dict[int, object] = {}

    def __repr__(self) -> str:
        return "ArbitratedExactCache()"

    def resolve_pinned(self, reference, scope):
        return self._cache.resolve_pinned(reference, scope)

    def resolve_compatible(self, scope, value_kind, source_digest, *, limit):
        return self._cache.resolve_compatible(scope, value_kind, source_digest, limit=limit)

    def lookup(self, key):
        thread = get_ident()
        self._release(thread)
        while True:
            result = self._cache.lookup(key)
            if result.state is not CacheLookupState.MISS:
                return result
            with self._lock:
                claim = self._claims.get(key)
                if claim is None:
                    self._claims[key] = _Claim()
                    self._held[thread] = key
                    return result
            self._wait(claim)

    def put_if_absent(self, entry):
        try:
            return self._cache.put_if_absent(entry)
        finally:
            self._release(get_ident())

    def _wait(self, claim: _Claim) -> None:
        check = self._checks.get(get_ident())
        # Cancellation, lease loss and sibling failures stay observable while waiting.
        while not claim.done.wait(self._poll):
            if check is not None:
                check()
        if claim.error is not None:
            raise claim.error

    def _release(self, thread: int, error: BaseException | None = None) -> None:
        with self._lock:
            key = self._held.pop(thread, None)
            claim = None if key is None else self._claims.pop(key, None)
        if claim is not None:
            claim.error = error
            claim.done.set()

    @contextmanager
    def worker(self, check=None):
        """Bind one document task's thread; its claim never outlives the task."""
        thread = get_ident()
        with self._lock:
            self._checks[thread] = check
        try:
            yield self
        except BaseException as error:
            self._release(thread, error)
            raise
        finally:
            self._release(thread)
            with self._lock:
                self._checks.pop(thread, None)
