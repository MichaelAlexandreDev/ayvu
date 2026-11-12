from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from ayvu.formats import ExportFileState, ExportVisibleContent, ExportVisibleContentRequest, InspectContent, InspectContentRequest, OutputDirectoryRef, SourceRevision, WriterPlan
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatService
from ayvu.jobs.execution import CancellationSource
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter
from zipfile import ZIP_DEFLATED

from tests.support.epub_fakes import ONE, OPF, TWO, epub_bytes
from ayvu.kernel import JobFailure, JobFailureFamily

from tests.support.format_use_cases_support import RecordingWriter, SOURCE_REF, _notes_registry
from tests.support.format_use_cases_support import epub_service, export_request, write_epub


def test_export_negotiates_capability_before_reading_content(tmp_path):
    path = tmp_path / "book.notes"
    path.write_text("not visible content\n", encoding="utf-8")
    source = LocalFormatSource(path)
    from tests.support.format_contract_env import CountingSource

    counted = CountingSource(source)
    writer = RecordingWriter()
    request = ExportVisibleContentRequest(SOURCE_REF, counted, None, OutputDirectoryRef(str(tmp_path / "out")))
    result = ExportVisibleContent(FormatService(_notes_registry()), writer).execute(request)

    assert (result.failure.family, result.failure.code) == (JobFailureFamily.CAPABILITY, "unsupported_operation")
    assert counted.reads == 1  # Bounded signature/extension detection only.
    assert writer.plan_calls == 0 and writer.published == []


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


def test_output_conflict_is_checked_before_source_read(tmp_path):
    path = write_epub(tmp_path / "source.epub")
    out = tmp_path / "out"
    out.mkdir()
    (out / "unrelated").write_text("keep", encoding="utf-8")
    from tests.support.format_contract_env import CountingSource

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
    from ayvu.formats.epub import adapter as epub_format

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


def test_a_book_with_awkward_chapter_names_exports_all_its_text(tmp_path):
    opf = OPF.replace(b"text/one.xhtml", b"text/con.xhtml").replace(b"text/two%20words.xhtml", b"text/c%2Ah.xhtml")
    members = {"OEBPS/content.opf": opf}
    source = epub_bytes(replace=members, drop=("OEBPS/text/one.xhtml", "OEBPS/text/two words.xhtml"),
                        add=(("OEBPS/text/con.xhtml", ONE, ZIP_DEFLATED), ("OEBPS/text/c*h.xhtml", TWO, ZIP_DEFLATED)))
    path = write_epub(tmp_path / "source.epub", source)
    out = tmp_path / "out"

    result = ExportVisibleContent(epub_service(), MarkdownDirectoryWriter()).execute(export_request(path, out))

    assert result.failure is None
    names = sorted(item.name for item in out.iterdir())
    assert any(name.endswith("-con.md") for name in names) and any(name.endswith("-c_h.md") for name in names)
    assert any("Second" in item.read_text() for item in out.iterdir())
