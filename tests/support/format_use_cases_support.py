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
from ayvu.formats import (
    CONTENT_BEFORE_PUBLICATION, ExportFileState, ExportVisibleContent, ExportVisibleContentRequest,
    InspectContent, InspectContentRequest, OutputDirectoryRef, PlannedOutputFile, PublishedFileRef,
    SourceRef, SourceRevision, WriterAuthorization, WriterPlan, WriterPublication,
)
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import (
    DetectionOutcome, FormatDescriptor, FormatError, FormatOperation, FormatOperationError,
    FormatSignature, VisibleContentDocument,
)
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatAdapter, FormatRegistration, FormatRegistry, FormatService
from ayvu.jobs.execution import CancellationSource
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter
from zipfile import ZIP_DEFLATED
from tests.support.epub_fakes import ONE, OPF, TWO, epub_bytes, opf_with
from tests.support.format_fakes import NOTES_REGISTRATION, NotesPorts, notes_adapter, notes_descriptor
from ayvu.kernel import JobFailure, JobFailureFamily

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


def _notes_registry() -> FormatRegistry:
    registry = FormatRegistry()
    registry.register(NOTES_REGISTRATION, notes_adapter())
    return registry


def _export_changing_source_after(path: Path, out: Path, change) -> object:
    """Runs ``change`` once the first file is out, so what stops the second or third file is the recheck."""

    class Writer(MarkdownDirectoryWriter):
        def publish(self, authorization, planned, document, source, *, before_publish):
            published = super().publish(authorization, planned, document, source, before_publish=before_publish)
            if planned.ordinal == 1:
                change()
            return published

    return ExportVisibleContent(epub_service(), Writer()).execute(export_request(path, out))


def _no_hard_links(code: int):
    def link(*_args, **_kwargs):
        raise OSError(code, "no hard links here")
    return link
