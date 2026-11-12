from __future__ import annotations

import errno
import os

import pytest

from ayvu.content_use_cases import ExportVisibleContent
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter


from tests.support.content_use_cases_support import _no_hard_links
from tests.support.content_use_cases_support import epub_service, export_request, write_epub


def test_exported_files_follow_the_umask_like_the_translated_epub(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    previous = os.umask(0o027)
    try:
        result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
            export_request(path, tmp_path / "out"))
    finally:
        os.umask(previous)
    assert result.failure is None
    assert {item.stat().st_mode & 0o777 for item in (tmp_path / "out").iterdir()} == {0o640}


def test_overwriting_keeps_what_the_old_file_allowed(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    assert ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(path, out)).failure is None
    target = sorted(out.iterdir())[0]
    target.chmod(0o604)
    target.write_text("old")

    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        export_request(path, out, overwrite=True))

    assert result.failure is None
    assert target.stat().st_mode & 0o777 == 0o604
    assert target.read_text() != "old"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_a_filesystem_without_hard_links_still_receives_the_files(tmp_path, monkeypatch, code):
    path = write_epub(tmp_path / "source.epub")
    expected_out = tmp_path / "expected"
    ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(path, expected_out))
    monkeypatch.setattr(os, "link", _no_hard_links(code))
    out = tmp_path / "out"

    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(path, out))

    assert result.failure is None and result.completed_count == 3
    assert {item.name: item.read_bytes() for item in out.iterdir()} == {
        item.name: item.read_bytes() for item in expected_out.iterdir()}


def test_without_hard_links_a_file_that_appeared_is_still_not_overwritten(tmp_path, monkeypatch):
    path = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    monkeypatch.setattr(os, "link", _no_hard_links(errno.EPERM))

    class Writer(MarkdownDirectoryWriter):
        def publish(self, authorization, planned, document, source, *, before_publish):
            def claim_the_name_first():
                (out / planned.token.name).write_text("mine")
                return before_publish()
            return super().publish(authorization, planned, document, source, before_publish=claim_the_name_first)

    result = ExportVisibleContent(epub_service(), Writer()).execute(export_request(path, out))

    assert result.failure.code == "output_conflict"
    assert [item.read_text() for item in out.iterdir() if not item.name.startswith(".ayvu-")] == ["mine"]
    assert not [item for item in out.iterdir() if item.name.startswith(".ayvu-")]


def test_a_refused_link_for_another_reason_is_still_a_failure(tmp_path, monkeypatch):
    path = write_epub(tmp_path / "source.epub")
    monkeypatch.setattr(os, "link", _no_hard_links(errno.EACCES))

    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        export_request(path, tmp_path / "out"))

    assert result.failure.code == "output_permission_denied"
