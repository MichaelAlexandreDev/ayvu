"""Wire the local review adapters to framework-free use cases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ...formats.epub.adapter import epub_format_adapter
from ...formats.local import LocalFormatSource
from ...formats import FormatService, FormatRegistry, FormatSource
from ...jobs import CancellationSource
from ...review import ReviewUseCasePorts, ExportReviewPackage, ValidateReviewPackage, ApplyReviewPackage

from .review_packages import LocalReviewPackagePort
from .review_start import _utc_now
from .review_operations import LocalReviewOperations


@dataclass(frozen=True, repr=False)
class ReviewUseCases:
    export: ExportReviewPackage
    validate: ValidateReviewPackage
    apply: ApplyReviewPackage
    operations: LocalReviewOperations

    def __repr__(self) -> str:
        return "ReviewUseCases()"

    def cancellation(self) -> CancellationSource:
        return self.operations.cancellation()


def epub_review_formats() -> FormatService:
    registry = FormatRegistry()
    from ...formats.epub.adapter import EPUB_REGISTRATION
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def open_review_use_cases(processing_dir: Path, *, source: FormatSource | None = None,
                          source_path: Path | str | None = None,
                          formats: FormatService | None = None,
                          clock: Callable[[], datetime] = _utc_now,
                          new_id: Callable[[], str] = lambda: str(uuid4())) -> ReviewUseCases:
    """Open framework-free use cases; repositories/adapters stay in this composition module."""
    format_service = formats or epub_review_formats()
    if source is None and source_path is not None:
        source = LocalFormatSource(source_path)
    packages = LocalReviewPackagePort(processing_dir, format_service, source=source)
    operations = LocalReviewOperations(processing_dir, format_service, clock=clock, new_id=new_id)
    ports = ReviewUseCasePorts(packages, operations)
    return ReviewUseCases(ExportReviewPackage(ports), ValidateReviewPackage(ports),
                         ApplyReviewPackage(ports), operations)
