"""Bounded errors shared by every module: messages never include caller data."""

from __future__ import annotations


class JobError(ValueError):
    """A bounded error: messages never include caller data."""


class JobValidationError(JobError):
    pass


class JobEventError(JobError):
    """An invalid event value or serialized form."""
