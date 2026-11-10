"""Time checks shared by every module."""

from __future__ import annotations

from datetime import datetime, timedelta

from .errors import JobValidationError


def validate_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise JobValidationError("invalid_timestamp")
