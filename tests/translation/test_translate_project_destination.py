"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import errno
import json
import os
from dataclasses import replace
import pytest
from ayvu.artifacts.destination import LocalDestinationPayloads
from ayvu.jobs.model import JobState
from tests.support.translate_project_support import world


def test_an_output_that_appeared_meanwhile_is_not_overwritten(world):
    def someone_else_writes(text):
        world.destination.parent.mkdir(exist_ok=True)
        world.destination.write_bytes(b"not ours")
    world.translator.on_call = someone_else_writes
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "output_exists"
    assert world.destination.read_bytes() == b"not ours" and world.leftovers() == []
    assert world.artifacts_of(world.all_jobs()[0]) == [("abandoned", "final_mismatch")]


def test_overwrite_replaces_the_file_and_keeps_its_mode(world):
    world.destination.parent.mkdir()
    world.destination.write_bytes(b"the old book")
    world.destination.chmod(0o640)
    result = world.run(overwrite=True)
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and world.destination.read_bytes() != b"the old book"
    assert (world.destination.stat().st_mode & 0o777) == 0o640 and world.leftovers() == []


def test_a_link_at_the_destination_stays_a_link(world):
    real = world.tmp_path / "real.epub"
    real.write_bytes(b"old")
    world.destination.parent.mkdir()
    world.destination.symlink_to(real)
    result = world.run(overwrite=True)
    assert result.state is JobState.VALIDATED
    assert world.destination.is_symlink() and real.read_bytes() != b"old"


def test_a_destination_that_is_the_source_is_refused(world):
    result = world.run(destination=world.source, overwrite=True)
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "output_is_source"


def test_a_link_to_the_source_is_refused(world):
    link = world.tmp_path / "alias.epub"
    link.symlink_to(world.source)
    result = world.run(destination=link, overwrite=True)
    world.assert_consistent(result)
    assert result.failure.code == "output_is_source"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_a_filesystem_without_hard_links_still_publishes(world, code):
    def no_links(*_):
        raise OSError(code, "no hard links here")
    world.ops.before["link"] = no_links
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.VALIDATED and world.leftovers() == []
    assert world.destination.stat().st_nlink == 1


def test_without_hard_links_an_output_that_appeared_meanwhile_is_still_not_overwritten(world):
    def no_links(*_):
        world.destination.parent.mkdir(exist_ok=True)
        world.destination.write_bytes(b"not ours")
        raise OSError(errno.EPERM, "no hard links here")
    world.ops.before["link"] = no_links
    result = world.run()
    assert result.failure.code == "output_exists" and world.destination.read_bytes() == b"not ours"
    assert world.leftovers() == []


def test_a_real_link_failure_is_reported_and_not_worked_around(world):
    def broken(*_):
        raise OSError(errno.EIO, "disk trouble")
    world.ops.before["link"] = broken
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "io" and not world.destination.exists()


def test_a_full_disk_while_writing_leaves_nothing_behind(world):
    def full(stream, data):
        raise OSError(errno.ENOSPC, "no space")
    world.ops.before["write"] = full
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and not world.destination.exists() and world.leftovers() == []


def test_two_runs_to_one_destination_cannot_both_win(world):
    # Different options, so the second is not a duplicate. It finishes first, while the first one waits on it.
    command = world.command()
    other = world.command(chunk_limit=500)
    finished = []
    def second_run(text):
        if not finished:
            finished.append(None)
            # Its own words, so the two outputs differ.
            world.translator.change = lambda item: f"OTHER:{item}"
            finished[0] = world.use_case(other).execute(other)
            world.translator.change = lambda item: f"PT:{item}"
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    second = finished[0]
    assert second.state is JobState.VALIDATED
    written = world.destination.read_bytes()
    assert first.state is JobState.FAILED and first.failure.code == "output_exists" and first.artifact is None
    assert world.destination.read_bytes() == written and world.leftovers() == []
    assert world.artifacts_of(world.jobs().get(first.job_id).job) == [("abandoned", "final_mismatch")]


def test_a_file_that_appeared_meanwhile_with_the_very_same_bytes_is_not_called_a_failure(world):
    # Reconciliation goes by the bytes, so the run cannot tell this from its own file, and they are the same book.
    command = world.command()
    other = world.command(chunk_limit=500)
    finished = []
    def second_run(text):
        if not finished:
            finished.append(None)
            finished[0] = world.use_case(other).execute(other)
    world.translator.on_call = second_run
    first = world.use_case(command).execute(command)
    assert finished[0].state is JobState.VALIDATED
    assert first.state is JobState.PARTIALLY_COMPLETED and first.outcome.summary.reason_code == "publication_uncommitted"
    assert first.failure.code == "output_exists" and world.leftovers() == []


def test_a_run_that_lost_its_lease_can_never_publish(world):
    world.translator.on_call = lambda text: world.clock.advance(hours=2)
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.FAILED and result.failure.code == "lease_expired"
    assert not world.destination.exists() and world.leftovers() == []


def test_a_lease_lost_just_after_the_file_became_visible_ends_with_the_truth_about_the_file(world):
    world.ops.after["link"] = lambda *_: world.clock.advance(hours=2)
    result = world.run()
    world.assert_consistent(result)
    assert result.state is JobState.PARTIALLY_COMPLETED and result.outcome.summary.reason_code == "publication_uncommitted"
    assert result.failure.code == "lease_expired" and result.output_ready
    assert world.destination.exists() and world.leftovers() == []
    assert world.artifacts_of(world.all_jobs()[0]) == [("ready", None)]


def test_the_journal_says_where_the_output_went_and_only_that(tmp_path):
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=tmp_path / "out" / "x.epub")
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    journal = tmp_path / "root" / "publications" / f"{artifact}.json"
    assert json.loads(journal.read_text()) == {"version": 1, "destination": str(tmp_path / "out" / "x.epub"), "replace": False}
    assert (journal.stat().st_mode & 0o777) == 0o600
    assert payloads.journal_ids() == (artifact,) and not payloads.publishing
    payloads.publish(artifact, size, digest)
    assert payloads.publishing and payloads.evidence(artifact, size, digest).final == "match"
    with payloads.open_verified(artifact, size, digest) as stream:
        assert stream.read() == b"bytes"
    payloads.forget(artifact)
    payloads.forget(artifact)
    assert payloads.journal_ids() == ()


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 2, "destination": "/x", "replace": false}',
                                     '{"version": 1, "destination": "relative", "replace": false}',
                                     '{"version": 1, "destination": "/x", "replace": "yes"}'])
def test_a_damaged_journal_is_refused_and_never_followed(tmp_path, content):
    from ayvu.artifacts.model import ArtifactPathError
    payloads = LocalDestinationPayloads(tmp_path / "root")
    artifact = "00000000-0000-4000-8000-000000000001"
    journal = tmp_path / "root" / "publications"
    journal.mkdir(parents=True)
    (journal / f"{artifact}.json").write_text(content)
    with pytest.raises(ArtifactPathError):
        payloads.evidence(artifact, 1, "sha256:" + "0" * 64)
    payloads.discard(artifact)
    assert payloads.journal_ids() == ()


def test_a_store_without_a_destination_can_only_settle(tmp_path):
    from ayvu.artifacts.model import ArtifactStoreError
    payloads = LocalDestinationPayloads(tmp_path)
    artifact = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ArtifactStoreError):
        payloads.write_staged(artifact, [b"x"], lambda: None)
    with pytest.raises(ArtifactStoreError):
        payloads.publish(artifact, 1, "sha256:" + "0" * 64)
    assert payloads.evidence(artifact, 1, "sha256:" + "0" * 64).final == "absent"


def test_a_symlink_swapped_in_at_the_destination_does_not_count_as_the_output(tmp_path):
    destination = tmp_path / "out.epub"
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=destination)
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    payloads.publish(artifact, size, digest)
    moved = tmp_path / "moved.epub"
    destination.rename(moved)
    destination.symlink_to(moved)
    from ayvu.artifacts.model import ArtifactPathError
    with pytest.raises(ArtifactPathError):
        payloads.evidence(artifact, size, digest)


def test_an_extra_name_on_the_output_means_it_cannot_be_trusted(tmp_path):
    destination = tmp_path / "out.epub"
    payloads = LocalDestinationPayloads(tmp_path / "root", destination=destination)
    artifact = "00000000-0000-4000-8000-000000000001"
    size, digest = payloads.write_staged(artifact, [b"bytes"], lambda: None)
    payloads.publish(artifact, size, digest)
    os.link(destination, tmp_path / "another-name")
    assert payloads.evidence(artifact, size, digest).final == "mismatch"
    assert payloads.evidence(artifact, 99, digest).final == "mismatch"
    destination.unlink()
    assert payloads.evidence(artifact, size, digest).final == "absent"
