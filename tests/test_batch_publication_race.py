"""Publication checks exercise real files without opening a project database."""
import os
import stat
from pathlib import Path

import pytest

from ayvu.artifact import ArtifactConflictError
from ayvu.artifact_destination import LocalDestinationPayloads
from ayvu.batch_output_policy import BatchOutputError, BatchOutputPolicy, OutputIntent
from ayvu.domain import OutputPlan

ARTIFACT = "00000000-0000-4000-8000-000000000001"


def make_plan(tmp_path, *, overwrite=False):
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    destination = tmp_path / "out" / "translated.epub"
    destination.parent.mkdir()
    if overwrite:
        destination.write_bytes(b"old")
        destination.chmod(0o604)
    intent = OutputIntent(source, OutputPlan(destination), tmp_path / "reports", "report", overwrite=overwrite)
    return source, destination, BatchOutputPolicy().plan((intent,)).items[0]


def staged(tmp_path, destination, guard, *, overwrite=False):
    payload = LocalDestinationPayloads(tmp_path / "journal", destination=destination,
                                       replace_existing=overwrite, before_publish=guard)
    size, digest = payload.write_staged(ARTIFACT, [b"translated"], lambda: None)
    return payload, size, digest


@pytest.mark.parametrize("changed", ["source", "destination"])
def test_changes_immediately_before_publication_are_refused(tmp_path, changed):
    source, destination, item = make_plan(tmp_path, overwrite=True)

    def change_then_check():
        (source if changed == "source" else destination).write_bytes(b"foreign content")
        item.publication_guard()

    payload, size, digest = staged(tmp_path, destination, change_then_check, overwrite=True)
    with pytest.raises(ArtifactConflictError, match="destination_changed"):
        payload.publish(ARTIFACT, size, digest)
    assert destination.read_bytes() == (b"old" if changed == "source" else b"foreign content")


def test_parent_replaced_with_symlink_before_guard_is_refused(tmp_path):
    _, destination, item = make_plan(tmp_path)
    moved = tmp_path / "moved"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / destination.name).write_bytes(b"foreign")

    def replace_parent_then_check():
        destination.parent.rename(moved)
        destination.parent.symlink_to(foreign, target_is_directory=True)
        item.publication_guard()

    payload, size, digest = staged(tmp_path, destination, replace_parent_then_check)
    with pytest.raises(ArtifactConflictError, match="destination_changed"):
        payload.publish(ARTIFACT, size, digest)
    assert (foreign / destination.name).read_bytes() == b"foreign"
    assert not (moved / destination.name).exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_publication_preserves_umask_or_existing_mode(tmp_path, overwrite):
    _, destination, item = make_plan(tmp_path, overwrite=overwrite)
    previous = os.umask(0o027)
    try:
        payload, size, digest = staged(tmp_path, destination, item.publication_guard, overwrite=overwrite)
        payload.publish(ARTIFACT, size, digest)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(destination.stat().st_mode) == (0o604 if overwrite else 0o640)
    assert destination.read_bytes() == b"translated"


def test_exclusive_creation_attempts_are_bounded_at_10000(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    _, _, item = make_plan(tmp_path)
    real_open = os.open
    attempts = []

    def occupied(path, flags, *args, **kwargs):
        if flags & os.O_EXCL and Path(path).suffix == ".md":
            attempts.append(Path(path).name)
            raise FileExistsError(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(policy.os, "open", occupied)
    with pytest.raises(BatchOutputError, match="auxiliary_allocation_exhausted"):
        item.write_auxiliary("report", "content")
    assert len(attempts) == 10_000
    assert attempts[0] == "report.md"
    assert attempts[-1] == "report-10000.md"


def test_case_insensitive_detection_uses_read_only_directory_lookup(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    directory = tmp_path / "MixedCase"
    directory.mkdir()
    source = directory / "source.epub"
    source.write_bytes(b"source")
    other = directory / "other.epub"
    other.write_bytes(b"other")
    original_stat = Path.stat
    alternate = directory.with_name(directory.name.swapcase())

    def simulated_stat(path, *args, **kwargs):
        if path == alternate:
            return original_stat(directory, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", simulated_stat)
    assert os.path.normcase("A") != os.path.normcase("a")  # Test the non-Windows detection path.
    one = OutputIntent(source, OutputPlan(directory / "Output.epub"), directory / "reports", "one")
    two = OutputIntent(other, OutputPlan(directory / "output.epub"), directory / "reports", "two")
    before = set(directory.iterdir())
    with pytest.raises(BatchOutputError, match="destination_collision"):
        BatchOutputPolicy().plan((one, two))
    assert set(directory.iterdir()) == before


@pytest.mark.parametrize("initially_exists", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_changed_report_directory_is_refused_before_auxiliary_write(tmp_path, initially_exists, dry_run):
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    reports = tmp_path / "reports"
    if initially_exists:
        reports.mkdir()
    intent = OutputIntent(source, OutputPlan(tmp_path / "output.epub", dry_run=dry_run), reports, "report")
    item = BatchOutputPolicy().plan((intent,)).items[0]
    if initially_exists:
        reports.rename(tmp_path / "original-reports")
    foreign = tmp_path / "foreign-reports"
    foreign.mkdir()
    reports.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(BatchOutputError, match="report_directory_changed"):
        item.write_auxiliary("report", "content")
    assert list(foreign.iterdir()) == []


def test_batch_created_report_directory_is_available_to_later_item(tmp_path):
    source1, source2 = tmp_path / "one.epub", tmp_path / "two.epub"
    source1.write_bytes(b"one")
    source2.write_bytes(b"two")
    reports = tmp_path / "new" / "reports"
    intents = tuple(OutputIntent(source, OutputPlan(tmp_path / (source.stem + "-out.epub"), dry_run=True),
                                 reports, source.stem) for source in (source1, source2))
    plan = BatchOutputPolicy().plan(intents)
    assert plan.items[0].write_auxiliary("report", "first").read_text() == "first"
    assert plan.items[1].write_auxiliary("report", "second").read_text() == "second"


def test_existing_report_directory_replaced_by_another_directory_is_refused(tmp_path):
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    reports = tmp_path / "reports"
    reports.mkdir()
    intent = OutputIntent(source, OutputPlan(tmp_path / "output.epub", dry_run=True), reports, "report")
    item = BatchOutputPolicy().plan((intent,)).items[0]
    reports.rename(tmp_path / "original-reports")
    reports.mkdir()
    with pytest.raises(BatchOutputError, match="report_directory_changed"):
        item.write_auxiliary("report", "content")
    assert list(reports.iterdir()) == []
