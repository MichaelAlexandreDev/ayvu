"""Local publication of the CLI's Markdown reports: where a report goes and how it is written."""
from __future__ import annotations

from pathlib import Path

from ...kernel import automatic_output_candidate


def publish_cli_report(path: Path, text: str) -> Path:
    """Preserve the current report publication semantics, including mode and overwrite behavior."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def report_destination(directory: Path, stem: str, suffix: str = '.md') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = automatic_output_candidate(directory, stem, suffix, 1)
    index = 2
    while path.exists():
        path = automatic_output_candidate(directory, stem, suffix, index)
        index += 1
    return path
