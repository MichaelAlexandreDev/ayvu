from __future__ import annotations


import pytest

from ayvu.content_use_cases import ExportVisibleContent, OutputDirectoryRef
from ayvu.formats.contracts import VisibleContentDocument
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter


from tests.support.content_use_cases_support import epub_service, export_request, write_epub


@pytest.mark.parametrize("stem,code", [
    ("/absolute/canary", "unsafe_output_name"), ("../traversal", "unsafe_output_name"),
])
def test_markdown_writer_refuses_unsafe_names_before_publishing_output(tmp_path, stem, code):
    source_path = tmp_path / "source.epub"
    source_path.write_bytes(b"source")
    source = LocalFormatSource(source_path)
    writer = MarkdownDirectoryWriter()
    authorization = writer.authorize(OutputDirectoryRef(str(tmp_path / "out")), overwrite=False)
    plan = writer.plan(authorization, source, (VisibleContentDocument("internal", stem, ("secret text",)),))
    assert plan.failure.code == code
    assert list((tmp_path / "out").iterdir()) == []


def test_markdown_writer_detects_canonical_name_collisions(tmp_path, monkeypatch):
    source_path = tmp_path / "source.epub"
    source_path.write_bytes(b"source")
    root = tmp_path / "out"
    writer = MarkdownDirectoryWriter()
    authorization = writer.authorize(OutputDirectoryRef(str(root)), overwrite=False)
    monkeypatch.setattr(MarkdownDirectoryWriter, "_name", staticmethod(lambda _ordinal, _stem: ("001-same.md", None)))
    plan = writer.plan(authorization, LocalFormatSource(source_path),
                       (VisibleContentDocument("one", "one", ()), VisibleContentDocument("two", "two", ())))
    assert plan.failure.code == "duplicate_output_target"
    assert list(root.iterdir()) == []


def test_markdown_writer_checks_canonical_containment_for_a_planned_target(tmp_path, monkeypatch):
    source_path = tmp_path / "source.epub"
    source_path.write_bytes(b"source")
    outside = tmp_path / "outside.md"
    outside.write_text("untouched", encoding="utf-8")
    root = tmp_path / "out"
    writer = MarkdownDirectoryWriter()
    authorization = writer.authorize(OutputDirectoryRef(str(root)), overwrite=True)
    monkeypatch.setattr(MarkdownDirectoryWriter, "_name", staticmethod(lambda _ordinal, _stem: ("../outside.md", None)))

    plan = writer.plan(authorization, LocalFormatSource(source_path),
                       (VisibleContentDocument("key", "chapter", ("private",)),))

    assert plan.failure.code == "output_path_escape"
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert list(root.iterdir()) == []


def test_markdown_writer_refuses_symlink_escape_and_source_alias(tmp_path):
    source_path = tmp_path / "source.epub"
    source_path.write_bytes(b"source")
    source = LocalFormatSource(source_path)
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("do not change", encoding="utf-8")
    target = root / "001-chapter.md"
    target.symlink_to(outside)
    writer = MarkdownDirectoryWriter()
    authorization = writer.authorize(OutputDirectoryRef(str(root)), overwrite=True)
    plan = writer.plan(authorization, source, (VisibleContentDocument("key", "chapter", ("secret",)),))
    assert plan.failure.code == "output_path_escape"
    assert outside.read_text(encoding="utf-8") == "do not change"
    target.unlink()

    root2 = tmp_path / "alias-out"
    root2.mkdir()
    (root2 / "001-chapter.md").hardlink_to(source_path)
    authorization2 = writer.authorize(OutputDirectoryRef(str(root2)), overwrite=True)
    plan2 = writer.plan(authorization2, source, (VisibleContentDocument("key", "chapter", ("secret",)),))
    assert plan2.failure.code == "source_alias"
    assert source_path.read_bytes() == b"source"

    root3 = tmp_path / "symlink-alias-out"
    root3.mkdir()
    (root3 / "001-chapter.md").symlink_to(source_path)
    authorization3 = writer.authorize(OutputDirectoryRef(str(root3)), overwrite=True)
    plan3 = writer.plan(authorization3, source, (VisibleContentDocument("key", "chapter", ("secret",)),))
    assert plan3.failure is not None
    assert source_path.read_bytes() == b"source"


def test_markdown_writer_normalizes_permission_failure_and_preserves_unrelated_files(tmp_path, monkeypatch):
    root = tmp_path / "out"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("untouched", encoding="utf-8")
    writer = MarkdownDirectoryWriter()
    monkeypatch.setattr("ayvu.formats.markdown.writer.os.access", lambda *_args, **_kwargs: False)
    denied = writer.authorize(OutputDirectoryRef(str(root)), overwrite=True)
    assert denied.failure.code == "output_permission_denied"
    monkeypatch.undo()

    source = write_epub(tmp_path / "book.epub")
    result = ExportVisibleContent(epub_service(), writer).execute(export_request(source, root, overwrite=True))
    assert result.failure is None and result.completed_count == 3
    assert marker.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("stem,expected", [
    ("con", "001-con.md"), ("CON", "001-CON.md"), ("bad:name", "001-bad_name.md"), ("c*h?", "001-c_h_.md"),
    ("a\\b", "001-a_b.md"), ("tab\there", "001-tab_here.md"), ("dot. ", "001-dot. .md"),
])
def test_a_chapter_name_never_stops_the_export(stem, expected):
    name, failure = MarkdownDirectoryWriter._name(1, stem)
    assert failure is None and name == expected


def test_a_chapter_name_too_long_for_a_file_is_shortened_on_a_character_boundary():
    name, failure = MarkdownDirectoryWriter._name(7, "ç" * 200)
    assert failure is None
    assert name.startswith("007-ç") and name.endswith("ç.md")
    assert len(name.encode("utf-8")) <= 255
