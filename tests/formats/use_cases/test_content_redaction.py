from __future__ import annotations



from ayvu.formats import ExportVisibleContent, ExportVisibleContentRequest, OutputDirectoryRef, PlannedOutputFile, PublishedFileRef, SourceRevision, WriterAuthorization, WriterPlan, WriterPublication
from ayvu.formats.contracts import VisibleContentDocument
from ayvu.formats.local import LocalFormatSource

from ayvu.kernel import JobFailure, JobFailureFamily

from tests.support.format_use_cases_support import RecordingWriter, SOURCE_REF
from tests.support.format_use_cases_support import epub_service, export_request, write_epub


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
    from ayvu.formats.epub import adapter as epub_format

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
