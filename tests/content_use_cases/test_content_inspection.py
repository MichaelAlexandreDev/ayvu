from __future__ import annotations

import sqlite3
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import requests

from ayvu.content_use_cases import ExportVisibleContent, ExportVisibleContentRequest, InspectContent, InspectContentRequest, OutputDirectoryRef
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatAdapter, FormatRegistration, FormatRegistry, FormatService
from ayvu.jobs.execution import CancellationSource

from tests.support.epub_fakes import OPF, epub_bytes
from tests.support.format_fakes import NOTES_REGISTRATION, NotesPorts, notes_adapter, notes_descriptor
from ayvu.kernel import JobFailureFamily

from tests.support.content_use_cases_support import RecordingWriter, SOURCE_REF
from tests.support.content_use_cases_support import epub_service, write_epub


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
    from tests.support.format_contract_env import CountingSource

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
    from tests.support.format_contract_env import CountingSource

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
    from ayvu.formats.epub.io import inspect_epub

    legacy = inspect_epub(path)
    assert (result.title, result.authors, result.language, result.document_count, result.item_count) == (
        legacy.title, tuple(legacy.authors), legacy.language, legacy.document_count, legacy.item_count,
    )
