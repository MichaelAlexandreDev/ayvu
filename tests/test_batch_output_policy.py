from pathlib import Path

import pytest

from ayvu.batch_output_policy import BatchOutputError, BatchOutputPolicy, OutputIntent
from ayvu.domain import OutputPlan


def intent(root: Path, name: str, *, dry=False, overwrite=False):
    source = root / f"{name}.epub"
    source.write_bytes(name.encode())
    return OutputIntent(source, OutputPlan(root / f"{name}-pt.epub", dry_run=dry), root / "reports",
                        f"{name}-pt-report", f"{name}-pt-missing", overwrite)


def test_plans_and_exclusively_allocates_without_creating_directory(tmp_path):
    one = intent(tmp_path, "one")
    plan = BatchOutputPolicy().plan((one,))
    assert not one.report_dir.exists()
    (one.report_dir).mkdir()
    (one.report_dir / "one-pt-report.md").write_text("old")
    item = plan.items[0]
    assert item.write_auxiliary("report", "new").name == "one-pt-report-2.md"
    assert (one.report_dir / "one-pt-report.md").read_text() == "old"
    assert item.write_auxiliary("missing", "missing").name == "one-pt-missing.txt"


def test_duplicate_and_source_aliases_refused(tmp_path):
    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    same = OutputIntent(two.source, one.output, two.report_dir, two.report_stem, overwrite=True)
    with pytest.raises(BatchOutputError, match="destination_collision"):
        BatchOutputPolicy().plan((one, same))
    swap = OutputIntent(one.source, OutputPlan(two.source), one.report_dir, one.report_stem, overwrite=True)
    with pytest.raises(BatchOutputError, match="source_collision"):
        BatchOutputPolicy().plan((swap, two))


def test_dry_run_does_not_block_existing_epub(tmp_path):
    item = intent(tmp_path, "book", dry=True)
    item.output.path.write_text("previous")
    planned = BatchOutputPolicy().plan((item,)).items[0]
    assert planned.write_auxiliary("report", "report").read_text() == "report"
    assert item.output.path.read_text() == "previous"


def test_source_and_target_changes_are_rejected(tmp_path):
    first = intent(tmp_path, "first")
    plan = BatchOutputPolicy().plan((first,)).items[0]
    first.output.path.write_text("raced")
    with pytest.raises(BatchOutputError, match="destination_changed"):
        plan.revalidate()
    first.output.path.unlink()
    first.source.unlink()
    first.source.mkdir()
    with pytest.raises(BatchOutputError, match="source_changed"):
        plan.revalidate()


def test_auxiliary_reservations_skip_sources_and_other_items(tmp_path):
    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    one = OutputIntent(one.source, one.output, tmp_path, "two", None)
    two = OutputIntent(two.source, two.output, tmp_path, "two", None)
    plan = BatchOutputPolicy().plan((one, two))
    assert plan.items[0].write_auxiliary("report", "one").name == "two.md"
    assert plan.items[1].write_auxiliary("report", "two").name == "two-2.md"


def test_hardlink_and_symlink_source_aliases(tmp_path):
    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    hardlink = tmp_path / "hardlink.epub"
    hardlink.hardlink_to(two.source)
    alias = OutputIntent(one.source, OutputPlan(hardlink), one.report_dir, one.report_stem, overwrite=True)
    with pytest.raises(BatchOutputError, match="source_collision"):
        BatchOutputPolicy().plan((alias, two))
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    alias = OutputIntent(one.source, OutputPlan(link / two.source.name), one.report_dir, one.report_stem, overwrite=True)
    with pytest.raises(BatchOutputError, match="source_collision"):
        BatchOutputPolicy().plan((alias, two))


def test_exclusive_allocation_exhaustion_is_bounded(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    one = intent(tmp_path, "one")
    item = BatchOutputPolicy().plan((one,)).items[0]
    monkeypatch.setattr(policy, "MAX_CANDIDATES", 2)
    one.report_dir.mkdir()
    (one.report_dir / "one-pt-report.md").write_text("existing")
    (one.report_dir / "one-pt-report-2.md").write_text("existing")
    with pytest.raises(BatchOutputError, match="auxiliary_allocation_exhausted"):
        item.write_auxiliary("report", "new")


def test_completed_output_identity_is_revalidated(tmp_path):
    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    plan = BatchOutputPolicy().plan((one, two))
    one.output.path.write_text("published")
    plan.items[0].mark_published()
    # Replace the inode without relying on inode reuse after unlink.
    replacement = tmp_path / "replacement.epub"
    replacement.write_text("foreign")
    replacement.replace(one.output.path)
    with pytest.raises(BatchOutputError, match="destination_changed"):
        plan.items[1].revalidate()


def test_concurrent_creator_wins_first_auxiliary_candidate(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    one = intent(tmp_path, "one")
    item = BatchOutputPolicy().plan((one,)).items[0]
    original_open = policy.os.open
    raced = []

    def race(path, flags, *args):
        if Path(path).name == "one-pt-report.md" and not raced:
            raced.append(True)
            Path(path).write_text("concurrent")
        return original_open(path, flags, *args)

    monkeypatch.setattr(policy.os, "open", race)
    output = item.write_auxiliary("report", "ours")
    assert raced == [True]
    assert output.name == "one-pt-report-2.md"
    assert (one.report_dir / "one-pt-report.md").read_text() == "concurrent"
    assert output.read_text() == "ours"


def test_in_place_changes_to_source_or_overwrite_target_invalidate_plan(tmp_path):
    one = intent(tmp_path, "one", overwrite=True)
    one.output.path.write_text("original")
    plan = BatchOutputPolicy().plan((one,)).items[0]
    one.output.path.write_text("new content")
    with pytest.raises(BatchOutputError, match="destination_changed"):
        plan.revalidate()
    plan = BatchOutputPolicy().plan((one,)).items[0]
    one.source.write_bytes(b"changed source")
    with pytest.raises(BatchOutputError, match="source_changed"):
        plan.revalidate()


def test_case_insensitive_identity_rejects_output_aliases(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    two = OutputIntent(two.source, OutputPlan(tmp_path / "ONE-PT.EPUB"), two.report_dir, two.report_stem)
    original_normcase = policy.os.path.normcase
    monkeypatch.setattr(policy.os.path, "normcase", lambda path: original_normcase(path).casefold())
    with pytest.raises(BatchOutputError, match="destination_collision"):
        BatchOutputPolicy().plan((one, two))


def test_case_insensitive_reservations_do_not_reuse_an_auxiliary_name(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    one = OutputIntent(one.source, one.output, one.report_dir, "Shared", None)
    two = OutputIntent(two.source, two.output, two.report_dir, "shared", None)
    original_normcase = policy.os.path.normcase
    monkeypatch.setattr(policy.os.path, "normcase", lambda path: original_normcase(path).casefold())
    plan = BatchOutputPolicy().plan((one, two))
    assert plan.items[0].write_auxiliary("report", "first").name == "Shared.md"
    assert plan.items[1].write_auxiliary("report", "second").name == "shared-2.md"


def test_planning_exhaustion_is_recorded_per_item_without_writing(tmp_path, monkeypatch):
    import ayvu.batch_output_policy as policy

    one, two = intent(tmp_path, "one"), intent(tmp_path, "two")
    one.report_dir.mkdir()
    (one.report_dir / "one-pt-report.md").write_text("existing")
    monkeypatch.setattr(policy, "MAX_CANDIDATES", 1)
    plan = BatchOutputPolicy().plan((one, two))
    assert plan.items[0].planning_failure == "auxiliary_allocation_exhausted"
    assert plan.items[1].planning_failure is None
    assert (one.report_dir / "one-pt-report.md").read_text() == "existing"
    assert plan.items[1].write_auxiliary("report", "second").read_text() == "second"
