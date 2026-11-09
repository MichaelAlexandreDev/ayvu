"""Composition root for the read-only content workflows."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .content_use_cases import (
    ExportVisibleContent, ExportVisibleContentRequest, ExportVisibleContentResult, InspectContent,
    InspectContentRequest, InspectContentResult, OutputDirectoryRef, SourceRef,
)
from .epub_format import EPUB_REGISTRATION, epub_format_adapter
from .format_local import LocalFormatSource
from .format_service import FormatRegistry, FormatService
from .markdown_writer import MarkdownDirectoryWriter


@dataclass(frozen=True, repr=False)
class ContentUseCases:
    inspect: InspectContent
    export_visible_content: ExportVisibleContent

    def __repr__(self) -> str:
        return "ContentUseCases()"

    def inspect_path(self, path: Path) -> InspectContentResult:
        return self.inspect.execute(InspectContentRequest(SourceRef("cli_input"), LocalFormatSource(path)))

    def export_path(self, source: Path, destination: Path, *, overwrite: bool = False) -> ExportVisibleContentResult:
        return self.export_visible_content.execute(ExportVisibleContentRequest(
            SourceRef("cli_input"), LocalFormatSource(source), None, OutputDirectoryRef(str(destination)), overwrite,
        ))


def open_content_use_cases() -> ContentUseCases:
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    formats = FormatService(registry)
    return ContentUseCases(InspectContent(formats), ExportVisibleContent(formats, MarkdownDirectoryWriter()))


__all__ = ["ContentUseCases", "LocalFormatSource", "open_content_use_cases"]
