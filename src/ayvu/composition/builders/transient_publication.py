"""Publish one local preview using staging, EPUB validation and atomic replace.

This adapter owns output I/O only. No artifact repository, execution store or
review package is involved, and provider failures never enter publication.
"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from ...formats import AliasVerdict, FormatContext, FormatOperation, FormatOperationError, FormatCancelledError
from ...formats.epub.adapter import epub_format_adapter
from ...formats.epub.validation import validate_output_epub
from ...formats.local import LocalOutputGuard
from ...kernel import JobFailure, JobFailureFamily
from ...translation import TranslationDryRunCancelled, TranslationExecutionError


def _refuse(code):
    raise TranslationExecutionError(JobFailure(JobFailureFamily.RESOURCE, code))


class LocalTransientPublication:
    def __init__(self, destination: Path, *, overwrite=False, before_publish=lambda: None):
        self.destination, self.overwrite, self.before_publish = destination, overwrite, before_publish
        self.validation = None

    def _staging_directory(self):
        for directory in self.destination.parents:
            if directory.is_dir():
                return directory
        _refuse("preview_publication_failed")

    def _check(self, source, cancellation):
        verdict = LocalOutputGuard(self.destination).verdict(source)
        if verdict is not AliasVerdict.DISTINCT:
            _refuse("output_is_source" if verdict is AliasVerdict.SAME else "output_alias_unknown")
        if self.destination.exists() and not self.overwrite:
            _refuse("output_exists")
        if cancellation is not None and cancellation.checkpoint("engine_before_publication") is not None:
            raise TranslationDryRunCancelled()

    def publish(self, source, request, *, cancellation=None):
        self._check(source, cancellation)
        self.before_publish()
        fingerprint = source.fingerprint()
        if any(document.source_fingerprint != fingerprint for document in request.documents):
            _refuse("source_changed")
        adapter = epub_format_adapter()
        context = FormatContext(source, fingerprint, adapter.descriptor.claim(FormatOperation.PREVIEW).safe_points, cancellation)
        staging, chunks = None, None
        try:
            # Delay directories and staging until the adapter has validated the complete EPUB.
            chunks = iter(adapter.previewer.preview(context, request))
            first = next(chunks)
            with NamedTemporaryFile(dir=self._staging_directory(), prefix=".ayvu-preview-", suffix=".epub", delete=False) as stream:
                staging = Path(stream.name)
                stream.write(first)
                for chunk in chunks:
                    stream.write(chunk)
            self.validation = validate_output_epub(staging)
            if not self.validation.ok and self.validation.document_count == 0:
                _refuse("invalid_output")
            if source.fingerprint() != fingerprint:
                _refuse("source_changed")
            self._check(source, cancellation)
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, self.destination)
            return self.validation
        except FormatOperationError as error:
            if error.failure.family is JobFailureFamily.CANCELLED:
                raise TranslationDryRunCancelled() from None
            raise TranslationExecutionError(error.failure) from None
        except FormatCancelledError:
            raise TranslationDryRunCancelled() from None
        except (OSError, StopIteration):
            _refuse("preview_publication_failed")
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
            if chunks is not None:
                chunks.close()
