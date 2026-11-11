"""Errors of translating one book, raised below the CLI and shown by it. Messages are bounded codes or fixed text."""

from __future__ import annotations


class PreflightError(RuntimeError):
    def __init__(self, summary: str, next_step: str, detail: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.next_step = next_step
        self.detail = detail


class PreflightPolicyError(PreflightError):
    """A policy refusal remains distinguishable without parsing presentation text."""

    def __init__(self, code: str, next_step: str, detail: str) -> None:
        super().__init__("O acesso HTTP ao tradutor foi bloqueado.", next_step, detail)
        self.code = code


class BookRefused(RuntimeError):
    """The book is refused before anything starts. The message is the code, never a name from the book."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TranslationDryRunCancelled(KeyboardInterrupt):
    """The no-project path observed a request at a callback safe point."""


class ProjectTranslationUnavailable(RuntimeError):
    """The project's storage could not be opened. Nothing was started, and the message is a bounded code."""


class BatchOutputError(ValueError):
    """A stable code, never a path or a system exception."""


class ResumeStateError(ValueError):
    """An unreadable or unsupported resume file. The message may name the file, never its content."""
