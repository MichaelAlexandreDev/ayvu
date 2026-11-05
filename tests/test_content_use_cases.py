from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from ayvu.content_use_cases import (
    CONTENT_BEFORE_PUBLICATION, ExportFileState, ExportVisibleContent, ExportVisibleContentRequest,
    InspectContent, InspectContentRequest, OutputDirectoryRef, PlannedOutputFile, PublishedFileRef,
    SourceRef, SourceRevision, WriterAuthorization, WriterPlan, WriterPublication,
)
from ayvu.epub_format import EPUB_REGISTRATION, epub_format_adapter
from ayvu.format_adapter import (
    DetectionOutcome, FormatDescriptor, FormatError, FormatOperation, FormatOperationError,
    FormatSignature, VisibleContentDocument,
)
from ayvu.format_local import LocalFormatSource
from ayvu.format_service import FormatAdapter, FormatRegistration, FormatRegistry, FormatService
from ayvu.job_events import JobFailure, JobFailureFamily
from ayvu.job_execution import CancellationSource
from ayvu.markdown_writer import MarkdownDirectoryWriter
from epub_fakes import epub_bytes, opf_with, OPF
from format_fakes import NOTES_REGISTRATION, NotesPorts, notes_adapter, notes_descriptor


SOURCE_REF = SourceRef("test_source")


def epub_service() -> FormatService:
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def write_epub(path: Path, data: bytes | None = None) -> Path:
    path.write_bytes(epub_bytes() if data is None else data)
    return path


def export_request(source: Path, destination: Path, *, revision: SourceRevision | None = None,
                   overwrite: bool = False, cancellation=None, progress=None) -> ExportVisibleContentRequest:
    return ExportVisibleContentRequest(SOURCE_REF, LocalFormatSource(source), revision,
                                       OutputDirectoryRef(str(destination)), overwrite, cancellation=cancellation,
                                       progress=progress)


class RecordingWriter:
    def __init__(self, fail_ordinal: int | None = None, *, on_publish=None) -> None:
        self.fail_ordinal = fail_ordinal
        self.on_publish = on_publish
        self.authorization = object()
        self.authorize_calls = 0
        self.plan_calls = 0
        self.published = []
        self.documents = ()

    def authorize(self, destination, *, overwrite):
        self.authorize_calls += 1
        return WriterAuthorization(token=self.authorization)

    def plan(self, authorization, source, documents):
        self.plan_calls += 1
        self.documents = documents
        return WriterPlan(files=tuple(PlannedOutputFile(index, object()) for index in range(1, len(documents) + 1)))

    def publish(self, authorization, planned, document, source, *, before_publish):
        if self.on_publish is not None:
            self.on_publish(planned.ordinal)
        failure = before_publish()
        if failure is not None:
            return WriterPublication(failure=failure)
        if self.fail_ordinal == planned.ordinal:
            return WriterPublication(failure=JobFailure(JobFailureFamily.RESOURCE, "output_write_failed"))
        self.published.append((planned.ordinal, document))
        return WriterPublication(published_file=PublishedFileRef(planned.ordinal, f"{planned.ordinal:03d}-stub.md"))

    def prepare_empty(self, authorization):
        return None


def test_inspection_uses_registry_and_keeps_the_source_read_only(minimal_epub_path, monkeypatch, tmp_path):
    source = LocalFormatSource(minimal_epub_path)
    before_hash = source.fingerprint()
    before_stat = minimal_epub_path.stat()
    network = []

    def forbidden_network(*_args, **_kwargs):
        network.append(True)
        raise AssertionError("read-only inspection attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("read-only inspection attempted directory creation")))
    monkeypatch.setattr(sqlite3, "connect", forbidden_network)
    result = InspectContent(epub_service()).execute(InspectContentRequest(SOURCE_REF, source))

    assert result.failure is None
    assert result.title == "Minimal Test Book"
    assert result.authors == ("Ayvu Tests",)
    assert result.language == "en"
    assert (result.document_count, result.item_count) == (3, 5)
    assert result.source_revision is not None and result.format_descriptor.adapter_id == "epub"
    assert result.inventory.document_count == 4  # Selection inventory includes the package title.
    assert (result.progress.phase, result.progress.completed, result.progress.total) == ("inspection", 1, 1)
    assert source.fingerprint() == before_hash
    after_stat = minimal_epub_path.stat()
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (before_stat.st_size, before_stat.st_mtime_ns)
    assert not (tmp_path / "inspection-output").exists()
    assert network == []


def test_cancellation_before_inspection_or_export_prevents_work(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_bytes(epub_bytes())
    source = LocalFormatSource(source_path)
    from format_contract import CountingSource

    inspect_case = InspectContent(epub_service())
    inspect_cancel = CancellationSource(inspect_case.safe_points("epub"))
    inspect_cancel.request("test_cancel", datetime.now(UTC))
    counted_for_inspection = CountingSource(source)
    inspected = inspect_case.execute(InspectContentRequest(SOURCE_REF, counted_for_inspection,
                                                            cancellation=inspect_cancel.token))
    assert (inspected.failure.family, inspected.failure.code) == (JobFailureFamily.CANCELLED, "cancelled")
    assert counted_for_inspection.reads == 0

    writer = RecordingWriter()
    export_case = ExportVisibleContent(epub_service(), writer)
    export_cancel = CancellationSource(export_case.safe_points("epub"))
    export_cancel.request("test_cancel", datetime.now(UTC))
    counted_for_export = CountingSource(source)
    exported = export_case.execute(ExportVisibleContentRequest(
        SOURCE_REF, counted_for_export, None, OutputDirectoryRef(str(tmp_path / "out")),
        cancellation=export_cancel.token,
    ))
    assert (exported.failure.family, exported.failure.code) == (JobFailureFamily.CANCELLED, "cancelled")
    assert counted_for_export.reads == 0
    assert writer.authorize_calls == 0 and writer.published == []


def test_cancellation_before_inspection_or_export_prevents_work(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_bytes(epub_bytes())
    source = LocalFormatSource(source_path)
    from format_contract import CountingSource

    inspect_case = InspectContent(epub_service())
    inspect_cancel = CancellationSource(inspect_case.safe_points("epub"))
    inspect_cancel.request("test_cancel", datetime.now(UTC))
    counted_for_inspection = CountingSource(source)
    inspected = inspect_case.execute(InspectContentRequest(SOURCE_REF, counted_for_inspection,
                                                            cancellation=inspect_cancel.token))
    assert (inspected.failure.family, inspected.failure.code) == (JobFailureFamily.CANCELLED, "cancelled")
    assert counted_for_inspection.reads == 0

    writer = RecordingWriter()
    export_case = ExportVisibleContent(epub_service(), writer)
    export_cancel = CancellationSource(export_case.safe_points("epub"))
    export_cancel.request("test_cancel", datetime.now(UTC))
    counted_for_export = CountingSource(source)
    exported = export_case.execute(ExportVisibleContentRequest(
        SOURCE_REF, counted_for_export, None, OutputDirectoryRef(str(tmp_path / "out")),
        cancellation=export_cancel.token,
    ))
    assert (exported.failure.family, exported.failure.code) == (JobFailureFamily.CANCELLED, "cancelled")
    assert counted_for_export.reads == 0
    assert writer.authorize_calls == 0 and writer.published == []


def test_inspection_keeps_unsupported_ambiguous_and_corrupt_distinct(tmp_path):
    unsupported_path = tmp_path / "book.txt"
    unsupported_path.write_bytes(b"not an EPUB")
    unsupported = InspectContent(epub_service()).execute(
        InspectContentRequest(SOURCE_REF, LocalFormatSource(unsupported_path)))
    assert (unsupported.failure.family, unsupported.failure.code) == (JobFailureFamily.CAPABILITY, "unsupported_format")

    adapter_a = notes_adapter()
    descriptor_b = replace(notes_descriptor(), adapter=replace(notes_descriptor().adapter, namespace="fake_notes_b"))
    ports_b = NotesPorts()
    adapter_b = FormatAdapter(descriptor_b, inspector=ports_b, extractor=ports_b)
    registry = FormatRegistry()
    registry.register(NOTES_REGISTRATION, adapter_a)
    registry.register(FormatRegistration("fake_notes_b", 1, descriptor_b.locators), adapter_b)
    ambiguous_path = tmp_path / "ambiguous.notes"
    ambiguous_path.write_bytes(b"one note\n")
    ambiguous = InspectContent(FormatService(registry)).execute(
        InspectContentRequest(SOURCE_REF, LocalFormatSource(ambiguous_path)))
    assert (ambiguous.failure.family, ambiguous.failure.code) == (JobFailureFamily.CAPABILITY, "ambiguous_format")

    corrupt_path = tmp_path / "corrupt.epub"
    corrupt_path.write_bytes(b"PK\x03\x04" + b"\0" * 26 + b"mimetypeapplication/epub+zip")
    corrupt = InspectContent(epub_service()).execute(InspectContentRequest(SOURCE_REF, LocalFormatSource(corrupt_path)))
    assert (corrupt.failure.family, corrupt.failure.code) == (JobFailureFamily.INTEGRITY, "corrupt_archive")


def test_inspection_preserves_missing_metadata_as_typed_missing_values(tmp_path):
    opf_without_metadata = OPF
    for metadata in (
        b'<dc:title id="t">The Title</dc:title>',
        b'<dc:creator opf:role="aut">An Author</dc:creator>',
        b'<dc:creator>Another</dc:creator>',
        b'<dc:language>en</dc:language>',
        b'<item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>',
    ):
        opf_without_metadata = opf_without_metadata.replace(metadata, b"")
    path = write_epub(tmp_path / "missing-metadata.epub",
                      epub_bytes(replace={"OEBPS/content.opf": opf_without_metadata}))
    result = InspectContent(epub_service()).execute(InspectContentRequest(SOURCE_REF, LocalFormatSource(path)))

    assert result.failure is None
    assert result.title is None and result.authors == () and result.language is None
    assert result.document_count == 3 and result.item_count == 5
    from ayvu.epub_io import inspect_epub

    legacy = inspect_epub(path)
    assert (result.title, result.authors, result.language, result.document_count, result.item_count) == (
        legacy.title, tuple(legacy.authors), legacy.language, legacy.document_count, legacy.item_count,
    )


def test_export_negotiates_capability_before_reading_content(tmp_path):
    path = tmp_path / "book.notes"
    path.write_text("not visible content\n", encoding="utf-8")
    source = LocalFormatSource(path)
    from format_contract import CountingSource

    counted = CountingSource(source)
    writer = RecordingWriter()
    request = ExportVisibleContentRequest(SOURCE_REF, counted, None, OutputDirectoryRef(str(tmp_path / "out")))
    result = ExportVisibleContent(FormatService(_notes_registry()), writer).execute(request)

    assert (result.failure.family, result.failure.code) == (JobFailureFamily.CAPABILITY, "unsupported_operation")
    assert counted.reads == 1  # Bounded signature/extension detection only.
    assert writer.plan_calls == 0 and writer.published == []


def _notes_registry() -> FormatRegistry:
    registry = FormatRegistry()
    registry.register(NOTES_REGISTRATION, notes_adapter())
    return registry


def test_export_plan_failure_marks_every_known_file_not_started(tmp_path):
    source_path = write_epub(tmp_path / "source.epub")
    writer = RecordingWriter()
    writer.plan = lambda *_args: WriterPlan(failure=JobFailure(JobFailureFamily.POLICY, "unsafe_output_name"))
    result = ExportVisibleContent(epub_service(), writer).execute(export_request(source_path, tmp_path / "out"))

    assert result.failure.code == "unsafe_output_name"
    assert [item.state for item in result.outcomes] == [ExportFileState.NOT_STARTED] * 3
    assert result.completed_count == result.failed_count == 0
    assert result.source_revision is not None
    assert not (tmp_path / "out").exists()


def test_export_reports_partial_publication_and_cancellation(tmp_path):
    source_path = write_epub(tmp_path / "source.epub")
    writer = RecordingWriter(fail_ordinal=2)
    failed = ExportVisibleContent(epub_service(), writer).execute(export_request(source_path, tmp_path / "failed-out"))
    assert [item.state for item in failed.outcomes] == [ExportFileState.COMPLETED, ExportFileState.FAILED,
                                                         ExportFileState.NOT_STARTED]
    assert failed.partial_publication and failed.failure.code == "output_write_failed"
    assert failed.warnings[0].code == "partial_publication"

    class CancelAfterFirst:
        def __init__(self, source):
            self.source = source
            self.done = False
            self.seen = []

        def report(self, progress):
            self.seen.append(progress)
            if progress.phase == "writing" and progress.completed == 1 and not self.done:
                self.source.request("test_cancel", datetime.now(UTC))
                self.done = True

    worker = ExportVisibleContent(epub_service(), RecordingWriter())
    token_source = CancellationSource(worker.safe_points("epub"))
    progress = CancelAfterFirst(token_source)
    cancelled = worker.execute(export_request(source_path, tmp_path / "cancelled-out", cancellation=token_source.token,
                                              progress=progress))
    assert [item.state for item in cancelled.outcomes] == [ExportFileState.COMPLETED,
                                                            ExportFileState.NOT_STARTED,
                                                            ExportFileState.NOT_STARTED]
    assert (cancelled.failure.family, cancelled.failure.code) == (JobFailureFamily.CANCELLED, "cancelled")
    assert cancelled.partial_publication
    assert [(item.phase, item.completed, item.total) for item in progress.seen] == [
        ("planning", 0, None), ("planning", 0, 3), ("writing", 0, 3), ("writing", 1, 3),
    ]


def test_export_validates_an_expected_source_revision_before_planning(tmp_path):
    source = write_epub(tmp_path / "source.epub")
    writer = RecordingWriter()
    wrong_revision = SourceRevision("sha256:" + "0" * 64)
    result = ExportVisibleContent(epub_service(), writer).execute(
        export_request(source, tmp_path / "out", revision=wrong_revision))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTEGRITY, "source_changed")
    assert writer.plan_calls == 0 and writer.published == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("stem,code", [
    ("/absolute/canary", "unsafe_output_name"), ("../traversal", "unsafe_output_name"),
    ("CON", "reserved_output_name"), ("bad:name", "unsafe_output_name"),
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
    monkeypatch.setattr("ayvu.markdown_writer.os.access", lambda *_args, **_kwargs: False)
    denied = writer.authorize(OutputDirectoryRef(str(root)), overwrite=True)
    assert denied.failure.code == "output_permission_denied"
    monkeypatch.undo()

    source = write_epub(tmp_path / "book.epub")
    result = ExportVisibleContent(epub_service(), writer).execute(export_request(source, root, overwrite=True))
    assert result.failure is None and result.completed_count == 3
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_export_content_and_path_canaries_stay_out_of_default_representations(tmp_path):
    path_canary = "/tmp/AYVU-UNRELATED-PATH-CANARY"
    text_canary = "AYVU-PRIVATE-CONTENT-CANARY"
    source = write_epub(tmp_path / "source.epub")
    writer = RecordingWriter()
    writer.plan = lambda *_args: WriterPlan(failure=JobFailure(JobFailureFamily.POLICY, "unsafe_output_name"))
    request = ExportVisibleContentRequest(SOURCE_REF, LocalFormatSource(source), None,
                                          OutputDirectoryRef(path_canary))
    result = ExportVisibleContent(epub_service(), writer).execute(request)
    combined = "\n".join((repr(request), repr(result), repr(result.outcomes), repr(result.failure)))
    assert path_canary not in combined and text_canary not in combined
    assert "source.epub" not in combined


def test_adapter_exception_canaries_stay_out_of_failures_and_logs(minimal_epub_path, monkeypatch, caplog, tmp_path):
    from ayvu import epub_format

    def fail_with_sensitive_exception(_self, _context):
        raise RuntimeError("BOOK-CONTENT-CANARY /tmp/UNRELATED-PATH-CANARY")

    monkeypatch.setattr(epub_format._EpubPorts, "extract_visible_content", fail_with_sensitive_exception)
    result = ExportVisibleContent(epub_service(), RecordingWriter()).execute(
        export_request(minimal_epub_path, tmp_path / "OUT-CANARY"))
    rendered = f"{result!r} {result.failure!r} {caplog.text}"
    assert result.failure.code == "adapter_failed"
    assert "BOOK-CONTENT-CANARY" not in rendered
    assert "/tmp/UNRELATED-PATH-CANARY" not in rendered
    assert "OUT-CANARY" not in rendered


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

    monkeypatch.setattr("ayvu.markdown_writer.os.link", checked_link)
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

    monkeypatch.setattr("ayvu.markdown_writer.os.replace", checked_replace)
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

    monkeypatch.setattr("ayvu.markdown_writer.os.replace", fail_second)
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


def test_output_conflict_is_checked_before_source_read(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    out.mkdir()
    (out / "unrelated").write_text("keep", encoding="utf-8")
    from format_contract import CountingSource

    source = CountingSource(LocalFormatSource(path))
    request = ExportVisibleContentRequest(SOURCE_REF, source, None, OutputDirectoryRef(str(out)))
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(request)
    assert result.failure.code == "output_not_empty"
    assert source.reads == 0
    assert (out / "unrelated").read_text(encoding="utf-8") == "keep"


def test_corrupt_and_hostile_archives_never_publish_output_files(tmp_path):
    corrupt = tmp_path / "corrupt.epub"
    corrupt.write_bytes(b"PK\x03\x04" + b"\0" * 26 + b"mimetypeapplication/epub+zip")
    corrupt_out = tmp_path / "corrupt-out"
    corrupt_result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(corrupt, corrupt_out))
    assert corrupt_result.failure.code == "corrupt_archive"
    assert list(corrupt_out.iterdir()) == []

    hostile = tmp_path / "hostile.epub"
    hostile.write_bytes(epub_bytes(add=[("../outside.md", b"canary", 0)]))
    hostile_out = tmp_path / "hostile-out"
    hostile_result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(hostile, hostile_out))
    assert hostile_result.failure.code == "unsafe_member_name"
    assert list(hostile_out.iterdir()) == []


def test_export_resource_limits_are_reported_before_publication(minimal_epub_path, tmp_path, monkeypatch):
    from dataclasses import replace as replace_dataclass
    from ayvu import epub_format

    monkeypatch.setattr(epub_format, "DEFAULT_LIMITS", replace_dataclass(epub_format.DEFAULT_LIMITS,
                                                                          max_document_bytes=32))
    base_archive = epub_format.EpubArchive

    class LimitedArchive(base_archive):
        def __init__(self, stream, *, checkpoint=None):
            super().__init__(stream, limits=epub_format.DEFAULT_LIMITS, checkpoint=checkpoint)

    monkeypatch.setattr(epub_format, "EpubArchive", LimitedArchive)
    out = tmp_path / "limited-out"
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(minimal_epub_path, out))
    assert result.failure.family is JobFailureFamily.RESOURCE
    assert result.failure.code == "document_too_large"
    assert result.outcomes == ()
    assert list(out.iterdir()) == []


def test_changed_source_is_refused_at_the_publication_boundary(minimal_epub_path, tmp_path):
    path = minimal_epub_path
    original = path.read_bytes()
    source = LocalFormatSource(path)
    original_fingerprint = source.fingerprint()
    calls = 0
    source_hash = source.fingerprint

    def changed_hash():
        nonlocal calls
        calls += 1
        return source_hash() if calls <= 2 else "sha256:" + "f" * 64

    source.fingerprint = changed_hash
    out = tmp_path / "changed-out"
    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(
        ExportVisibleContentRequest(SOURCE_REF, source, None, OutputDirectoryRef(str(out))))
    assert (result.failure.family, result.failure.code) == (JobFailureFamily.INTEGRITY, "source_changed")
    assert result.source_revision.value == original_fingerprint
    assert [item.state for item in result.outcomes] == [ExportFileState.FAILED,
                                                         ExportFileState.NOT_STARTED,
                                                         ExportFileState.NOT_STARTED]
    assert result.completed_count == 0 and not result.partial_publication
    assert not list(out.glob("*.md"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_fingerprint.removeprefix("sha256:")
    assert path.read_bytes() == original


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


def test_application_values_redact_revisions_documents_and_paths(tmp_path):
    secret = "/tmp/UNRELATED-ABSOLUTE-CANARY"
    revision = SourceRevision("sha256:" + "a" * 64)
    request = ExportVisibleContentRequest(SOURCE_REF, LocalFormatSource(secret), revision,
                                          OutputDirectoryRef(secret))
    document = VisibleContentDocument(secret, "chapter", ("BOOK-CONTENT-CANARY",))
    authorization = WriterAuthorization(token=object())
    plan = WriterPlan(files=(PlannedOutputFile(1, object()),))
    publication = WriterPublication(published_file=PublishedFileRef(1, secret))
    visible = " ".join(map(repr, (request, document, authorization, plan, publication, revision)))
    assert secret not in visible and "BOOK-CONTENT-CANARY" not in visible


@pytest.mark.parametrize("damage", [{"drop": ("mimetype",)}, {"replace": {"mimetype": b"text/plain"}}],
                         ids=["missing", "wrong"])
def test_a_defective_mimetype_does_not_stop_a_book_that_translate_accepts(tmp_path, damage):
    path = write_epub(tmp_path / "source.epub", epub_bytes(**damage))
    healthy = write_epub(tmp_path / "healthy.epub")
    out, healthy_out = tmp_path / "out", tmp_path / "healthy-out"

    inspected = InspectContent(epub_service()).execute(InspectContentRequest(SOURCE_REF, LocalFormatSource(path)))
    exported = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(path, out))
    ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(healthy, healthy_out))

    assert inspected.failure is None and inspected.title
    assert exported.failure is None and exported.completed_count
    assert {item.name: item.read_bytes() for item in out.iterdir()} == {
        item.name: item.read_bytes() for item in healthy_out.iterdir()}


def test_a_file_with_the_extension_that_is_no_book_is_reported_as_damaged(tmp_path):
    path = tmp_path / "source.epub"
    path.write_bytes(b"not a zip")

    result = InspectContent(epub_service()).execute(InspectContentRequest(SOURCE_REF, LocalFormatSource(path)))

    assert result.failure.code == "corrupt_archive"


def _export_changing_source_after(path: Path, out: Path, change) -> object:
    """Runs ``change`` once the first file is out, so what stops the second or third file is the recheck."""

    class Writer(MarkdownDirectoryWriter):
        def publish(self, authorization, planned, document, source, *, before_publish):
            published = super().publish(authorization, planned, document, source, before_publish=before_publish)
            if planned.ordinal == 1:
                change()
            return published

    return ExportVisibleContent(epub_service(), Writer()).execute(export_request(path, out))


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


def _no_hard_links(code: int):
    def link(*_args, **_kwargs):
        raise OSError(code, "no hard links here")
    return link


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
