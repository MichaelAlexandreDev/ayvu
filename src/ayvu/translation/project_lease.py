"""Concurrent renewal of an active project job lease."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from threading import Event, Thread

from ..jobs import JobLeaseError
from ..kernel import JobError


class _LeaseKeeper:
    """Renews from its own thread, since one chapter or one blocked call can outlast any safe point.

    Losing the lease stops nothing by itself: the artifact store checks it, so a run that lost it can
    never publish. A store that is busy for a moment is simply tried again on the next round.
    """

    def __init__(self, renew: Callable[[], None], interval: timedelta) -> None:
        self._renew = renew
        self._interval = interval.total_seconds()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="ayvu-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._renew()
            except JobLeaseError:
                return
            except JobError:
                continue
