"""Remote egress consent in the project database (#379): schema version 5, its record and revocation."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from ayvu.projects.consent import REMOTE_EGRESS_TEXT_VERSION, RemoteConsentError, SqliteRemoteConsentRepository
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_remote_consents, project_migration_runner
from ayvu.kernel.sqlite import SqliteMigrationRunner
from tests.support.paths import TESTS_ROOT

PROJECT, OTHER = str(UUID(int=1)), str(UUID(int=2))
NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)


def seeded(path, version):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((TESTS_ROOT / "fixtures" / "migrations" / f"version-{version}.sql").read_text())


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT * FROM remote_egress_consents ORDER BY project_id, provider").fetchall()


def test_a_fresh_database_records_only_provider_time_and_text_version(tmp_path):
    database = tmp_path / "project.sqlite"
    consents = open_remote_consents(database, tmp_path / "backups", PROJECT, clock=lambda: NOW)
    assert consents.current("anthropic") is None
    granted = consents.grant("anthropic")
    assert (granted.provider, granted.granted_at, granted.text_version) == ("anthropic", NOW, "remote-egress-v1")
    assert consents.current("anthropic") == granted and consents.list() == (granted,)
    assert rows(database) == [(PROJECT, "anthropic", "2026-11-18T12:30:00.000000+00:00", "remote-egress-v1")]
    assert REMOTE_EGRESS_TEXT_VERSION == "remote-egress-v1"


def test_an_old_database_is_upgraded_with_a_backup_and_keeps_its_jobs(tmp_path):
    database, backups = tmp_path / "project.sqlite", tmp_path / "backups"
    seeded(database, 4)
    with closing(sqlite3.connect(database)) as connection:
        before = connection.execute("SELECT name FROM sqlite_schema WHERE name = 'remote_egress_consents'").fetchall()
    assert before == []
    consents = open_remote_consents(database, backups, PROJECT, clock=lambda: NOW)
    consents.grant("openai")
    assert [item.provider for item in consents.list()] == ["openai"]
    [backup] = backups.iterdir()
    with closing(sqlite3.connect(backup)) as snapshot:
        assert snapshot.execute("PRAGMA user_version").fetchone() == (4,)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_a_database_at_version_four_is_refused_by_the_repository_until_migrated(tmp_path):
    database = tmp_path / "project.sqlite"
    seeded(database, 4)
    repository = SqliteRemoteConsentRepository(project_migration_runner(), database, PROJECT)
    with pytest.raises(RemoteConsentError, match="^version_gap$"):
        repository.list()
    older = SqliteRemoteConsentRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS[:4]), database, PROJECT)
    with pytest.raises(RemoteConsentError):
        older.list()


def test_a_consent_to_another_text_version_does_not_count_and_is_renewed(tmp_path):
    times = iter((NOW, NOW + timedelta(days=1)))
    consents = open_remote_consents(tmp_path / "project.sqlite", tmp_path / "backups", PROJECT, clock=lambda: next(times))
    consents.grant("anthropic", "remote-egress-v0")
    assert consents.current("anthropic") is None
    renewed = consents.grant("anthropic")
    assert consents.list() == (renewed,) and renewed.granted_at == NOW + timedelta(days=1)


def test_revoking_removes_one_provider_or_all_and_only_this_project(tmp_path):
    database, backups = tmp_path / "project.sqlite", tmp_path / "backups"
    mine = open_remote_consents(database, backups, PROJECT, clock=lambda: NOW)
    theirs = open_remote_consents(database, backups, OTHER, clock=lambda: NOW)
    for provider in ("anthropic", "openai"):
        mine.grant(provider)
    theirs.grant("anthropic")
    assert mine.revoke("openai") == 1 and [item.provider for item in mine.list()] == ["anthropic"]
    assert mine.revoke("openai") == 0
    assert mine.revoke() == 1 and mine.list() == ()
    assert [item.provider for item in theirs.list()] == ["anthropic"]


@pytest.mark.parametrize("provider", ["", "Anthropic", "../x", "a" * 65, None])
def test_invalid_providers_and_clocks_are_refused(tmp_path, provider):
    consents = open_remote_consents(tmp_path / "project.sqlite", tmp_path / "backups", PROJECT,
                                    clock=lambda: datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-3))))
    with pytest.raises(RemoteConsentError, match="^invalid_provider$"):
        consents.grant(provider)
    with pytest.raises(RemoteConsentError, match="^invalid_timestamp$"):
        consents.grant("anthropic")
    assert consents.list() == ()
