"""Content-free migration planning and outcomes, independent of SQLite/filesystems."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

ERROR_CODES = frozenset({
    'invalid_registry', 'newer_schema', 'version_gap', 'checksum_mismatch',
    'unmanaged_database', 'integrity_failed', 'recovery_required', 'unsupported_journal',
    'unsafe_path', 'busy', 'deadline', 'io', 'permission', 'backup_failed',
    'backup_limit', 'sql_failure', 'publication_uncertain', 'resource_limit',
})


class MigrationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_CODES else 'io'
        super().__init__(self.code)


@dataclass(frozen=True)
class MigrationPlan:
    versions: tuple[int, ...]
    target: int

    def __post_init__(self) -> None:
        if (type(self.versions) is not tuple or not 1 <= len(self.versions) <= 64
                or any(type(value) is not int for value in self.versions)
                or self.versions != tuple(range(1, len(self.versions) + 1))
                or type(self.target) is not int or self.target not in self.versions):
            raise MigrationError('invalid_registry')

    def pending(self, current: int) -> tuple[int, ...]:
        if type(current) is not int or current < 0:
            raise MigrationError('version_gap')
        if current > self.target:
            raise MigrationError('newer_schema')
        return tuple(value for value in self.versions if current < value <= self.target)


@dataclass(frozen=True)
class MigrationResult:
    status: str
    from_version: int | None
    to_version: int | None
    applied: tuple[int, ...] = ()
    error: str | None = None
    backup_name: str | None = field(default=None, repr=False)
    backup_verified: bool = False

    def __post_init__(self) -> None:
        if self.status not in {'created', 'upgraded', 'unchanged', 'failed'}:
            raise MigrationError('invalid_registry')
        if self.error is not None and self.error not in ERROR_CODES:
            raise MigrationError('invalid_registry')
        if self.backup_name is not None and not re.fullmatch(r'v\d{8}-to-\d{8}-\d{6}\.sqlite', self.backup_name):
            raise MigrationError('unsafe_path')

    @property
    def ok(self) -> bool:
        return self.status != 'failed'
