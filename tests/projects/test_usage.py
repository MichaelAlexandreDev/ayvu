"""Provider usage in the project database: schema version 6, one row per request, counts only (#380)."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ayvu.jobs.store import SqliteJobRepository
from ayvu.kernel.sqlite import SqliteMigrationRunner
from ayvu.projects.schema import PROJECT_MIGRATIONS, open_job_repository, open_provider_usage, project_migration_runner
from ayvu.projects.usage import ProviderUsageError, SqliteProviderUsageRepository
from ayvu.providers import UsageRecord, UsageTotals
from tests.projects.test_project_schema import request, seeded

PROJECT = str(UUID(int=1))
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def record(request_id="r1", job_id="job-1", **changes):
    values = dict(request_id=request_id, job_id=job_id, document_id="doc:v1:" + "a" * 64,
                  unit_id="unit:v1:" + "b" * 64, provider="ollama", model="qwen2.5:7b", characters=40, words=7,
                  input_tokens=300, output_tokens=60, cached_input_tokens=0, cost_micro_usd=0,
                  price_version="2026-11-20", price_source="table")
    return UsageRecord(**{**values, **changes})


def columns(database):
    with closing(sqlite3.connect(database)) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(provider_usage)")]


def test_a_fresh_database_gets_the_usage_table_at_version_six(tmp_path):
    database = tmp_path / "project.sqlite"
    usage = open_provider_usage(database, tmp_path / "backups", PROJECT, clock=lambda: NOW)
    assert usage.totals(("job-1",)) == UsageTotals()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
    # Counts only: no column could hold a translated or source text.
    assert columns(database) == [
        "request_id", "project_id", "job_id", "document_id", "unit_id", "provider", "model", "characters", "words",
        "input_tokens", "output_tokens", "cached_input_tokens", "cost_micro_usd", "price_version", "price_source",
        "recorded_at"]


def test_a_version_five_database_is_upgraded_with_a_backup_and_keeps_its_jobs(tmp_path):
    database, backups = tmp_path / "project.sqlite", tmp_path / "backups"
    seeded(database, 5)
    jobs = SqliteJobRepository(SqliteMigrationRunner(PROJECT_MIGRATIONS[:5]), database, PROJECT)
    created = jobs.create(request())
    usage = open_provider_usage(database, backups, PROJECT)
    usage.record(record())
    [backup] = backups.iterdir()
    with closing(sqlite3.connect(backup)) as snapshot:
        assert snapshot.execute("PRAGMA user_version").fetchone() == (5,)
        assert snapshot.execute("SELECT name FROM sqlite_schema WHERE name = 'provider_usage'").fetchall() == []
    assert usage.totals(("job-1",)).calls == 1
    assert open_job_repository(database, backups, PROJECT).get(created.job_id).job == created


def test_a_request_is_recorded_once_whoever_records_it(tmp_path):
    usage = open_provider_usage(tmp_path / "project.sqlite", tmp_path / "backups", PROJECT)
    assert usage.record(record()) is True
    assert usage.record(record(input_tokens=999)) is False  # another worker, or a replay: not counted again
    assert usage.records("job-1") == (record(),)


def test_totals_cover_the_jobs_asked_for_and_one_unknown_part_is_unknown(tmp_path):
    usage = open_provider_usage(tmp_path / "project.sqlite", tmp_path / "backups", PROJECT)
    usage.record(record("r1", "job-1", cost_micro_usd=10))
    usage.record(record("r2", "job-2", cost_micro_usd=20))
    usage.record(record("r3", "job-3", cost_micro_usd=None, output_tokens=None))
    assert usage.totals(("job-2", "job-1")) == UsageTotals(2, 80, 14, 600, 120, 0, 30)
    every = usage.totals(("job-1", "job-2", "job-3"))
    assert (every.calls, every.input_tokens, every.output_tokens, every.cost_micro_usd) == (3, 900, None, None)


def test_rows_belong_to_their_project(tmp_path):
    database, backups = tmp_path / "project.sqlite", tmp_path / "backups"
    open_provider_usage(database, backups, PROJECT).record(record())
    other = open_provider_usage(database, backups, str(UUID(int=2)))
    assert other.totals(("job-1",)) == UsageTotals() and other.records("job-1") == ()


def test_a_database_below_version_six_is_refused_by_the_repository_until_migrated(tmp_path):
    database = tmp_path / "project.sqlite"
    seeded(database, 5)
    with pytest.raises(ProviderUsageError):
        SqliteProviderUsageRepository(project_migration_runner(), database, PROJECT).record(record())


@pytest.mark.parametrize("job_ids", [(), "job-1", tuple(f"job-{index}" for index in range(65))])
def test_totals_refuse_an_invalid_job_list(tmp_path, job_ids):
    usage = open_provider_usage(tmp_path / "project.sqlite", tmp_path / "backups", PROJECT)
    with pytest.raises(ProviderUsageError, match="^invalid_job_ids$"):
        usage.totals(job_ids)
