"""SQLite migration runner and its contracts (D4 of the package-structure decision)."""

from __future__ import annotations

from .contracts import (
    ERROR_CODES, MigrationError, MigrationPlan, MigrationResult,
)
from .migrations import (
    APPLICATION_ID, MAX_DATABASE_BYTES, MAX_BACKUPS, LEDGER, SqliteMigration, sqlite_failure_code,
    SqliteMigrationRunner,
)

__all__ = [
    "APPLICATION_ID",
    "ERROR_CODES",
    "LEDGER",
    "MAX_BACKUPS",
    "MAX_DATABASE_BYTES",
    "MigrationError",
    "MigrationPlan",
    "MigrationResult",
    "SqliteMigration",
    "SqliteMigrationRunner",
    "sqlite_failure_code",
]
