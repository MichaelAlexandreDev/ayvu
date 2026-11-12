"""Recovery over the production stores, the real state files and the real CLI, under a fake translator.

A "crash" here is an exception nothing in production catches, raised from the translator. It leaves a job running
with a live lease and a guard nobody holds, as a killed process does. A real kill is in the concurrency tests.
"""
from __future__ import annotations
import json
import logging
import sqlite3
import pytest
from ayvu.jobs.model import JobState
from ayvu.recovery.service import CompatibilityStatus, RequiredDecision, ResumeStatus
from ayvu.translation.resume_reuse import RecoveryReason
from ayvu.v1.resume_scan import MAX_LEGACY_STATE_BYTES
from tests.support.cli_translate_project_support import _book
from tests.support.job_recovery_integration_support import CANARY_URL, Runner, Scene, _finished
from tests.support.job_recovery_integration_support import _isolated_from_the_user, _v1, _write, no_network
from tests.support.job_recovery_integration_support import scene, tree


def test_an_intact_output_is_verified_from_its_bytes(scene):
    _finished(scene)
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert not {RecoveryReason.ARTIFACT_MISSING, RecoveryReason.ARTIFACT_PARTIAL, RecoveryReason.ARTIFACT_CORRUPT, RecoveryReason.DESTINATION_CONFLICT} & set(seen.entry.reasons)
    assert RequiredDecision.REPLACE_DESTINATION in seen.required_decisions


@pytest.mark.parametrize("damage, reason", [
    (lambda path: path.unlink(), RecoveryReason.ARTIFACT_MISSING),
    (lambda path: path.write_bytes(path.read_bytes()[:-5]), RecoveryReason.ARTIFACT_PARTIAL),
    (lambda path: path.write_bytes(b"Z" + path.read_bytes()[1:]), RecoveryReason.ARTIFACT_CORRUPT),
], ids=["missing", "partial", "corrupt"])
def test_a_damaged_output_has_its_own_reason_and_can_be_redone(scene, damage, reason):
    _finished(scene)
    damage(scene.output)
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert reason in seen.entry.reasons and seen.entry.resumable
    assert scene.request().result.state is JobState.VALIDATED


def test_a_file_at_the_destination_with_no_record_is_a_conflict_that_needs_a_decision(scene):
    scene.interrupted_state()
    scene.output.write_bytes(b"not ours")
    seen = scene.recovery().inspect.execute(scene.only_entry().reference)
    assert RecoveryReason.DESTINATION_CONFLICT in seen.entry.reasons and RequiredDecision.REPLACE_DESTINATION in seen.required_decisions
    refused = scene.request(accepted=(RequiredDecision.FULL_REEXECUTION,))
    assert refused.code == "decision_required" and scene.output.read_bytes() == b"not ours"


def test_an_old_file_is_inspectable_gets_no_identity_and_is_not_touched(scene, tmp_path, no_network):
    path = _write(tmp_path, "book-pt.ayvu-state.json", _v1(tmp_path))
    before = tree(tmp_path)

    entry = scene.only_entry()
    seen = scene.recovery().inspect.execute(entry.reference)

    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and entry.reasons == (RecoveryReason.LEGACY_ONLY,)
    assert (entry.project_id, entry.job_id, entry.expected_revision) == (None, None, None)
    assert (entry.progress.total_chapters, entry.progress.completed_chapters, entry.progress.failed_chapters, entry.progress.failed_segments) == (3, 1, 1, 2)
    assert seen.selection is None and seen.provider_calls_may_repeat
    assert tree(tmp_path) == before and path.exists()


def test_an_old_file_resumes_only_through_the_current_flow_and_stays_untouched(scene, tmp_path):
    path = _write(tmp_path, "book-pt.ayvu-state.json", _v1(tmp_path))
    before = path.read_bytes()
    runner = Runner(scene)

    result = scene.request(runner=runner)

    assert result.status is ResumeStatus.LEGACY_REEXECUTION and runner.legacy == [result.reference]
    assert not (tmp_path / "processing" / "projects").exists() and path.read_bytes() == before


def test_an_old_file_with_a_project_but_no_job_on_its_plan_stays_legacy(scene, tmp_path):
    scene.save_state()
    scene.project()
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and entry.project_id is not None and entry.job_id is None


def test_a_changed_book_with_a_project_known_elsewhere_is_refused_on_the_legacy_path_too(tmp_path):
    scene = Scene(tmp_path)
    scene.project()
    _book(scene.source, chapter_two="<p>Now it is another book.</p>")
    other = Scene(tmp_path, source=scene.source, url="http://another.example.test")
    other.save_state()

    entry = other.only_entry()
    seen = other.recovery().inspect.execute(entry.reference)

    assert entry.compatibility is CompatibilityStatus.LEGACY_ONLY and RecoveryReason.SOURCE_CHANGED in seen.entry.reasons and not seen.entry.resumable


@pytest.mark.parametrize("name, content, reason", [
    ("bad.ayvu-state.json", b"\xff\xfe{}", RecoveryReason.INVALID_STATE_FILE),
    ("json.ayvu-state.json", b"{not json", RecoveryReason.INVALID_STATE_FILE),
    ("list.ayvu-state.json", b"[]", RecoveryReason.INVALID_STATE_FILE),
    ("field.ayvu-state.json", b'{"version": 1}', RecoveryReason.INVALID_STATE_FILE),
    ("newer.ayvu-state.json", b'{"version": 2}', RecoveryReason.UNSUPPORTED_SCHEMA),
    ("huge.ayvu-state.json", b" " * (MAX_LEGACY_STATE_BYTES + 1), RecoveryReason.INVALID_STATE_FILE),
], ids=["not_utf8", "not_json", "not_an_object", "missing_field", "newer_version", "too_large"])
def test_a_bad_file_is_one_unsupported_entry_and_is_left_alone(scene, tmp_path, name, content, reason):
    path = _write(tmp_path, name, content)
    before = tree(tmp_path)
    entry = scene.only_entry()
    assert entry.compatibility is CompatibilityStatus.UNSUPPORTED and entry.reasons == (reason,) and not entry.resumable
    assert tree(tmp_path) == before and path.read_bytes() == content


def test_a_directory_or_link_by_that_name_is_not_read(scene, tmp_path):
    (tmp_path / "processing").mkdir()
    (tmp_path / "processing" / "dir.ayvu-state.json").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_v1(tmp_path)))
    (tmp_path / "processing" / "link.ayvu-state.json").symlink_to(target)

    entries = scene.recovery().list_interrupted.execute().entries

    assert [entry.compatibility for entry in entries] == [CompatibilityStatus.UNSUPPORTED] * 2


def test_nothing_identifying_reaches_a_result_an_event_or_a_log(tmp_path, caplog):
    canary = "Canary Book Title"
    folder = tmp_path / canary
    folder.mkdir()
    scene = Scene(tmp_path, source=_book(folder / f"{canary}.epub"), output=folder / f"{canary} out.epub")
    state_file = scene.interrupted_state()
    with caplog.at_level(logging.DEBUG):
        recovery = scene.recovery()
        listing = recovery.list_interrupted.execute()
        entry = listing.entries[0]
        seen = recovery.inspect.execute(entry.reference)
        result = scene.request(recovery=recovery)
    legacy = recovery.evidence.legacy_entry(entry.reference)
    text = " ".join(repr(item) for item in (listing, entry, seen, seen.entry, seen.selection, result, recovery, recovery.evidence, legacy,
                                            result.inspection, *result.result.events))
    text += " ".join(entry.reference for entry in listing.entries) + caplog.text
    for secret in (canary, str(folder), CANARY_URL, "canary.example", str(state_file), "PT:", "Hello reader"):
        assert secret not in text


def test_the_error_from_a_bad_file_quotes_nothing_from_it(tmp_path):
    from ayvu.v1.resume_scan import LegacyResumeError, inspect_legacy_resume
    path = _write(tmp_path, "x.ayvu-state.json", {"version": 1, "status": "Canary status"})
    with pytest.raises(LegacyResumeError) as caught:
        inspect_legacy_resume(path)
    assert "Canary" not in str(caught.value) and "x.ayvu" not in repr(caught.value)


def test_a_job_list_can_be_read_newest_first_and_ready_artifacts_listed(scene):
    _finished(scene)
    scene.save_state()
    scene.request()
    project = scene.project()
    repository = scene.jobs_repository()
    newest = repository.list_jobs(project.project_id, newest_first=True)
    oldest = repository.list_jobs(project.project_id)
    assert [item.job.job_id for item in newest] == [item.job.job_id for item in reversed(oldest)]
    assert newest[0].job.continuation_of == oldest[0].job.job_id
    paths = scene.projects.resolve_database_paths(project.project_id)
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT count(*) FROM job_artifacts WHERE state = 'ready'").fetchone() == (2,)


def test_a_link_at_the_destination_stays_a_link_while_the_plan_holds_the_real_path(tmp_path):
    real = tmp_path / "real.epub"
    real.write_bytes(b"old")
    link = tmp_path / "link.epub"
    link.symlink_to(real)
    scene = Scene(tmp_path, output=link)
    assert scene.translate(overwrite=True).result.state is JobState.VALIDATED
    assert link.is_symlink() and real.read_bytes() != b"old"


def test_a_guard_that_cannot_be_read_clears_nobody(scene, tmp_path):
    scene.interrupted_state()
    job = scene.jobs()[0]
    guard = scene.projects.run_guard(job.request.project_id)
    lock = guard._path(job.request.input_fingerprint)
    lock.unlink()
    (tmp_path / "elsewhere").write_text("x")
    lock.symlink_to(tmp_path / "elsewhere")
    entry = scene.only_entry()
    assert RecoveryReason.ACTIVE_LEASE in entry.reasons and not entry.resumable


def test_the_role_recovery_looks_for_is_the_one_the_rich_path_writes():
    from ayvu.recovery.store import RAW_RESULTS_ROLE as read
    from ayvu.translation.raw_results import RAW_RESULTS_ROLE as written
    assert read == written
