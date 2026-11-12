from __future__ import annotations

import os
from datetime import UTC, datetime


from ayvu.content_use_cases import ExportFileState, ExportVisibleContent, ExportVisibleContentRequest, OutputDirectoryRef
from ayvu.formats.local import LocalFormatSource
from ayvu.jobs.execution import CancellationSource
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter

from ayvu.kernel import JobFailureFamily

from tests.support.content_use_cases_support import RecordingWriter, SOURCE_REF, _export_changing_source_after
from tests.support.content_use_cases_support import epub_service, export_request, write_epub


def test_production_writer_publishes_exact_markdown_atomically_and_keeps_original(minimal_epub_path, tmp_path, monkeypatch):
    source = LocalFormatSource(minimal_epub_path)
    original = source.fingerprint()
    out = tmp_path / "out"
    writes = []
    real_link = __import__("os").link

    def checked_link(staged, target, **kwargs):
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        descriptor = os.open(staged, os.O_RDONLY, dir_fd=kwargs["src_dir_fd"])
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            assert stream.read() in {
                "Chapter One\n\nHello reader. Visit\n\nchapter two\n\n.\n",
                "Chapter Two\n\nGoodbye reader.\n",
                "Minimal Test Book\n\nChapter One\n\nChapter Two\n",
        }
        writes.append(target)
        return real_link(staged, target, **kwargs)

    monkeypatch.setattr("ayvu.formats.markdown.writer.os.link", checked_link)
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(minimal_epub_path, out))
    assert result.failure is None and result.completed_count == 3
    assert writes == ["001-chapter1.md", "002-chapter2.md", "003-nav.md"]
    assert [(out / name).read_text(encoding="utf-8") for name in writes] == [
        "Chapter One\n\nHello reader. Visit\n\nchapter two\n\n.\n",
        "Chapter Two\n\nGoodbye reader.\n",
        "Minimal Test Book\n\nChapter One\n\nChapter Two\n",
    ]
    assert source.fingerprint() == original
    assert not list(out.glob("*.part"))


def test_production_writer_keeps_existing_files_and_overwrites_only_planned_files(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    out.mkdir()
    unrelated = out / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        export_request(path, out, overwrite=True))
    assert result.failure is None and result.completed_count == 3
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert sorted(item.name for item in out.iterdir()) == ["001-nav.md", "002-one.md", "003-two words.md", "keep.txt"]
    assert (out / "002-one.md").read_text(encoding="utf-8") == "Hello\n\nworld\n"


def test_overwrite_uses_a_complete_same_directory_stage_and_atomic_replace(tmp_path, monkeypatch):
    source = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    out.mkdir()
    target = out / "001-nav.md"
    target.write_text("old content", encoding="utf-8")
    unrelated = out / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    real_replace = os.replace
    publications = []

    expected = {
        "001-nav.md": b"First\n",
        "002-one.md": b"Hello\n\nworld\n",
        "003-two words.md": b"Second\n\nchapter\n",
    }

    def checked_replace(staged, final, **kwargs):
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        descriptor = os.open(staged, os.O_RDONLY, dir_fd=kwargs["src_dir_fd"])
        with os.fdopen(descriptor, "rb") as stream:
            staged_bytes = stream.read()
        assert staged_bytes == expected[final]
        publications.append(final)
        return real_replace(staged, final, **kwargs)

    monkeypatch.setattr("ayvu.formats.markdown.writer.os.replace", checked_replace)
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        export_request(source, out, overwrite=True))
    assert result.failure is None and result.completed_count == 3
    assert publications == ["001-nav.md", "002-one.md", "003-two words.md"]
    assert target.read_bytes() == b"First\n"
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_filesystem_failure_reports_per_file_state_and_never_publishes_partial_file(tmp_path, monkeypatch, caplog):
    source = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    out.mkdir()
    marker = out / "keep.txt"
    marker.write_text("unrelated", encoding="utf-8")
    real_replace = os.replace

    def fail_second(staged, target, **kwargs):
        if target == "002-one.md":
            raise OSError("/tmp/PATH-CANARY BOOK-CONTENT-CANARY")
        return real_replace(staged, target, **kwargs)

    monkeypatch.setattr("ayvu.formats.markdown.writer.os.replace", fail_second)
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        export_request(source, out, overwrite=True))
    assert [item.state for item in result.outcomes] == [ExportFileState.COMPLETED,
                                                        ExportFileState.FAILED,
                                                        ExportFileState.NOT_STARTED]
    assert result.failure.code == "output_write_failed" and result.partial_publication
    assert (out / "001-nav.md").read_text(encoding="utf-8") == "First\n"
    assert not (out / "002-one.md").exists() and not (out / "003-two words.md").exists()
    assert marker.read_text(encoding="utf-8") == "unrelated"
    assert not list(out.glob("*.part"))
    assert "/tmp/PATH-CANARY" not in repr(result) and "BOOK-CONTENT-CANARY" not in repr(result)
    assert "/tmp/PATH-CANARY" not in caplog.text and "BOOK-CONTENT-CANARY" not in caplog.text


def test_production_writer_rechecks_a_real_source_revision_before_publication(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    original = path.read_bytes()
    out = tmp_path / "changed-source-out"

    class ChangeAfterExtractionWriter(MarkdownDirectoryWriter):
        def plan(self, authorization, source, documents):
            path.write_bytes(original + b" changed after extraction")
            return super().plan(authorization, source, documents)

    result = ExportVisibleContent(epub_service(), ChangeAfterExtractionWriter()).execute(export_request(path, out))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTEGRITY, "source_changed")
    assert [item.state for item in result.outcomes] == [ExportFileState.FAILED,
                                                        ExportFileState.NOT_STARTED,
                                                        ExportFileState.NOT_STARTED]
    assert result.completed_count == 0 and not result.partial_publication
    assert list(out.iterdir()) == []
    assert path.read_bytes() == original + b" changed after extraction"


def test_cancellation_before_publication_does_not_publish_current_file(minimal_epub_path, tmp_path):
    writer = RecordingWriter()
    usecase = ExportVisibleContent(epub_service(), writer)
    cancellations = CancellationSource(usecase.safe_points("epub"))

    def cancel_before_publish(_ordinal):
        cancellations.request("test_cancel", datetime.now(UTC))

    writer.on_publish = cancel_before_publish
    result = usecase.execute(export_request(minimal_epub_path, tmp_path / "cancel-before-publish",
                                            cancellation=cancellations.token))
    assert result.failure.code == "cancelled"
    assert result.outcomes[0].state is ExportFileState.FAILED
    assert result.outcomes[0].failure.code == "cancelled"
    assert result.not_started_count == 2 and result.completed_count == 0


def test_a_size_change_between_publications_stops_the_next_file(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    original = path.read_bytes()

    result = _export_changing_source_after(path, tmp_path / "out", lambda: path.write_bytes(original + b"x"))

    assert result.failure.code == "source_changed"
    assert [item.state for item in result.outcomes] == [
        ExportFileState.COMPLETED, ExportFileState.FAILED, ExportFileState.NOT_STARTED]


def test_a_same_size_change_is_caught_by_the_last_publication(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    original = path.read_bytes()
    changed = original[:-1] + bytes([original[-1] ^ 1])

    result = _export_changing_source_after(path, tmp_path / "out", lambda: path.write_bytes(changed))

    assert result.failure.code == "source_changed"
    assert [item.state for item in result.outcomes] == [
        ExportFileState.COMPLETED, ExportFileState.COMPLETED, ExportFileState.FAILED]
    assert result.partial_publication


def test_publishing_does_not_hash_the_whole_book_once_per_file(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    source = LocalFormatSource(path)
    original_hash = source.fingerprint
    hashes = []

    def counted():
        hashes.append(1)
        return original_hash()

    source.fingerprint = counted

    class Writer(MarkdownDirectoryWriter):
        def plan(self, authorization, plan_source, documents):
            hashes.clear()
            return super().plan(authorization, plan_source, documents)

    result = ExportVisibleContent(epub_service(), Writer()).execute(
        ExportVisibleContentRequest(SOURCE_REF, source, None, OutputDirectoryRef(str(tmp_path / "out"))))

    assert result.failure is None and result.completed_count == 3
    assert len(hashes) == 2
