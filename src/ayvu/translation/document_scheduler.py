"""Bounded document admission and cooperative drain for the rich runtime.

A completed result occupies its slot until the coordinator consumes it. Failures
stop admission immediately, including failures beyond the next ordered result.
The executor remains alive until all started tasks have joined.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock


class DocumentWorkStopped(Exception):
    """A sibling failed; this task must leave without claiming completion."""


class DocumentScheduler:
    def __init__(self, checkpoint=lambda: None):
        self.stopping = Event()
        self._checkpoint = checkpoint
        self._lock = Lock()
        self._failures = {}

    def check(self):
        # Observe cancellation and lease loss even while draining a sibling.
        self._checkpoint()
        if self.stopping.is_set():
            raise DocumentWorkStopped()

    def observe(self):
        """The coordinator's check between results: a stop surfaces as the failure that caused it.

        The coordinator consumes each result outside ``ordered``. A sibling that fails meanwhile
        must stop the book with its own cause, not with the bare ``DocumentWorkStopped`` that
        tells a worker to leave.
        """
        try:
            self.check()
        except DocumentWorkStopped:
            self._raise_failure()

    def _work(self, item, execute):
        try:
            self.check()
            return execute(item, self.check)
        except DocumentWorkStopped:
            raise
        except BaseException as error:
            with self._lock:
                self._failures[item.index] = error
                self.stopping.set()
            raise

    def _raise_failure(self):
        with self._lock:
            failures = dict(self._failures)
        if failures:
            raise failures[min(failures)]
        raise DocumentWorkStopped()

    def ordered(self, items, workers, execute, admit=lambda item: None):
        """Yield in enumeration order, admitting at most ``workers`` documents.

        ``admit`` runs on the caller's thread, in enumeration order, before each
        document starts. The caller must close this iterator if its consumer
        raises. The finally block cancels queued tasks and joins started tasks.
        """
        if type(workers) is not int or workers < 1:
            raise ValueError("invalid_workers")
        if workers == 1:
            for item in items:
                admit(item)
                yield item, self._work(item, execute)
            return
        pending = deque()
        iterator = iter(items)
        threads = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ayvu-document")
        try:
            for _ in range(workers):
                self.check()
                item = next(iterator, None)
                if item is None:
                    break
                admit(item)
                pending.append((item, threads.submit(self._work, item, execute)))
            while pending:
                self.check()
                item, future = pending[0]
                result = future.result()
                self.check()
                yield item, result
                pending.popleft()
                # Resume only after the coordinator has consumed this result.
                self.check()
                following = next(iterator, None)
                if following is not None:
                    admit(following)
                    pending.append((following, threads.submit(self._work, following, execute)))
        except DocumentWorkStopped:
            self._raise_failure()
        finally:
            self.stopping.set()
            for _, future in pending:
                future.cancel()
            threads.shutdown(wait=True, cancel_futures=True)
