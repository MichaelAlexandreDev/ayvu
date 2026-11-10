"""The cooperative cancellation view an operation receives, shared by every module."""

from __future__ import annotations

from typing import Protocol

from .errors import JobError


class JobCancellationError(JobError):
    pass


class SafePointObserver(Protocol):
    """What a token reads from. The jobs module's ``CancellationSource`` is the implementation."""

    def _observe(self, safe_point: str) -> object | None: ...


class CancellationToken:
    """The only view an operation gets: a request is visible at safe points and nowhere else.

    ``checkpoint`` returns the jobs module's ``CancellationAcknowledgement``, or ``None``.
    """

    def __init__(self, source: SafePointObserver) -> None:
        self._source = source

    def __repr__(self) -> str:
        return "CancellationToken()"

    def checkpoint(self, safe_point: str) -> object | None:
        return self._source._observe(safe_point)
