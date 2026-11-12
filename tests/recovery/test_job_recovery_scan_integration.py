"""Recovery over the production stores, the real state files and the real CLI, under a fake translator.

A "crash" here is an exception nothing in production catches, raised from the translator. It leaves a job running
with a live lease and a guard nobody holds, as a killed process does. A real kill is in the concurrency tests.
"""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from ayvu.interfaces.cli.output import EXISTING_OUTPUT_OVERWRITE_OPTION
from ayvu.preferences.config_facade import AyvuConfig, ConfigStore, TranslationProfile, default_config_path
from ayvu.jobs.model import JobState
from ayvu.recovery.service import CompatibilityStatus
from ayvu.translation.resume_reuse import RecoveryReason
from ayvu.projects.schema import PROJECT_MIGRATIONS
from ayvu.recovery.checkpoint import ResumeStateStore
from ayvu.composition.builders.translate_project import open_job_recovery
from tests.support.cli_translate_project_support import _args, _book
from ayvu.kernel.sqlite import SqliteMigrationRunner
from tests.support.job_recovery_integration_support import Scene
from tests.support.job_recovery_integration_support import _interrupt_real_cli, _isolated_from_the_user, no_network
from tests.support.job_recovery_integration_support import scene, tree


def test_listing_and_inspecting_change_nothing_and_reach_nothing(scene, no_network):
    scene.interrupted_state()
    before = tree(scene.tmp_path)
    recovery = scene.recovery()

    entry = recovery.list_interrupted.execute().entries[0]
    seen = recovery.inspect.execute(entry.reference)

    assert seen.entry.resumable and tree(scene.tmp_path) == before
    assert scene.translator.calls.count("Goodbye reader.") == 1


def test_reading_an_empty_or_absent_folder_creates_nothing(tmp_path, no_network):
    recovery = open_job_recovery(tmp_path / "absent")
    assert recovery.list_interrupted.execute().entries == ()
    assert not (tmp_path / "absent").exists()
    (tmp_path / "empty").mkdir()
    open_job_recovery(tmp_path / "empty").list_interrupted.execute()
    assert list((tmp_path / "empty").iterdir()) == []


def test_reading_a_project_with_an_older_schema_does_not_upgrade_it(scene):
    scene.save_state()
    project = scene.project()
    paths = scene.projects.resolve_database_paths(project.project_id)
    SqliteMigrationRunner(PROJECT_MIGRATIONS[:3]).migrate(paths.database, paths.backups)
    before = tree(scene.processing)

    entries = scene.recovery().list_interrupted.execute().entries

    assert tree(scene.processing) == before
    assert {RecoveryReason.UNSUPPORTED_SCHEMA} <= {reason for entry in entries for reason in entry.reasons}


def test_a_project_with_no_database_yet_has_no_jobs(scene):
    scene.save_state()
    scene.project()
    assert scene.only_entry().compatibility is CompatibilityStatus.LEGACY_ONLY


def test_one_bad_project_does_not_hide_the_others(scene):
    scene.interrupted_state()
    bad = scene.processing / "projects" / "11111111-1111-4111-8111-111111111111"
    bad.mkdir()
    (bad / "project.json").write_text("{not json")
    (scene.processing / "projects" / ".staging-abc").mkdir()

    listing = scene.recovery().list_interrupted.execute()

    assert listing.unreadable_projects == 2
    assert [entry.compatibility for entry in listing.entries] == [CompatibilityStatus.DURABLE]


@pytest.mark.parametrize("more", [
    (), ("--chunk-limit", "500"), ("--chapters", "1-2"), ("--translate-metadata",), ("--translate-alt-text",), ("--translation-memory",), ("--fail-fast",),
    ("--overwrite",),
], ids=["plain", "chunk_limit", "chapters", "metadata", "alt_text", "memory", "fail_fast", "overwrite_flag"])
def test_an_interrupted_run_of_the_real_cli_is_found_from_its_file(tmp_path, monkeypatch, more):
    source = _book(tmp_path / "book.epub")
    if "--overwrite" in more:
        (tmp_path / "out.epub").write_bytes(b"old")
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, *more))
    assert entry.compatibility is CompatibilityStatus.DURABLE and entry.job_state is JobState.CANCELLED


def test_a_relative_output_is_found_from_its_file(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    arguments = _args(tmp_path, source, output=Path("relative-out.epub"))
    assert _interrupt_real_cli(tmp_path, monkeypatch, arguments).compatibility is CompatibilityStatus.DURABLE


def test_an_output_prompt_accepted_is_found_from_its_file(tmp_path, monkeypatch):
    """The file records the flag, which stayed off, and the plan holds what the person accepted at the prompt."""
    source = _book(tmp_path / "book.epub")
    (tmp_path / "out.epub").write_bytes(b"old")
    entry = _interrupt_real_cli(tmp_path, monkeypatch, ["--mode", "common", *_args(tmp_path, source)[2:]], stdin=f"{EXISTING_OUTPUT_OVERWRITE_OPTION}\n")
    assert entry.compatibility is CompatibilityStatus.DURABLE
    assert json.loads(next((tmp_path / "processing").glob("*.ayvu-state.json")).read_text())["overwrite"] is False


def test_a_profile_is_found_only_from_a_file_that_records_it(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore(path).save(AyvuConfig(profiles={"tec": TranslationProfile(target_language="pt", style="technical")}))
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, "--profile", "tec"))
    assert entry.compatibility is CompatibilityStatus.DURABLE
    state_file = next((tmp_path / "processing").glob("*.ayvu-state.json"))
    data = json.loads(state_file.read_text())
    assert data["profile_name"] == "tec"
    del data["profile_name"]
    state_file.write_text(json.dumps(data))
    assert open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0].compatibility is CompatibilityStatus.LEGACY_ONLY


def test_a_changed_glossary_is_a_different_configuration_and_a_missing_one_is_said(tmp_path, monkeypatch):
    source = _book(tmp_path / "book.epub")
    glossary = tmp_path / "g.json"
    glossary.write_text('{"Hello": "Ola"}')
    entry = _interrupt_real_cli(tmp_path, monkeypatch, _args(tmp_path, source, "--glossary", str(glossary)))
    assert entry.compatibility is CompatibilityStatus.DURABLE
    glossary.write_text('{"Hello": "Oi"}')
    changed = open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]
    assert changed.compatibility is CompatibilityStatus.LEGACY_ONLY and changed.job_id is None
    glossary.unlink()
    gone = open_job_recovery(tmp_path / "processing").list_interrupted.execute().entries[0]
    assert gone.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.MISSING_CONFIGURATION in gone.reasons


def test_an_address_holding_credentials_stays_on_the_legacy_path(tmp_path):
    scene = Scene(tmp_path, url="http://user:secret@translator.test")
    scene.save_state()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.MISSING_CONFIGURATION in entry.reasons
    assert "secret" not in repr(entry)


def test_a_file_that_finished_is_not_offered(scene):
    path = scene.save_state()
    state = ResumeStateStore(scene.processing).load(path)
    ResumeStateStore(scene.processing).save(state.mark_completed())
    assert scene.recovery().list_interrupted.execute().entries == ()


def test_two_books_with_one_name_share_a_file_and_the_first_is_kept_as_a_job(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = Scene(tmp_path, source=_book(tmp_path / "a" / "book.epub"), output=tmp_path / "a.epub")
    second = Scene(tmp_path, source=_book(tmp_path / "b" / "book.epub", chapter_two="<p>Goodbye other.</p>"), output=tmp_path / "b.epub")
    first.interrupted_state()
    second.interrupted_state()

    entries = first.recovery().list_interrupted.execute().entries

    assert sorted(entry.compatibility for entry in entries) == [CompatibilityStatus.DURABLE, CompatibilityStatus.JOB_ONLY]
