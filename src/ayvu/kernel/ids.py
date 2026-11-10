"""Identifier checks shared by every module."""

from __future__ import annotations

import re
from uuid import UUID

from .errors import JobValidationError

_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}")


def validate_job_identity(value: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise JobValidationError("invalid_job_identity") from None


def is_slug(value: object) -> bool:
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None
