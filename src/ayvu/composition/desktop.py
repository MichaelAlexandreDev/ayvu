"""Explicit, toolkit-free desktop composition and resource ownership.

Factories acquire one resource each as a context manager. They may use shared
application ports, but this module neither chooses concrete adapters nor opens
files or a network connection. A factory owns acquisition until it yields;
after that, the graph owns its context and closes it in reverse order.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from . import editions, surface_rules


@dataclass(frozen=True)
class DesktopSurface:
    """Effective action and recipe IDs, without product identity or toolkit objects.

    Actions reuse the shared command/menu binding IDs. They describe a plan;
    the installed desktop launcher still has no presentation implementation.
    """

    actions: tuple[str, ...]
    providers: tuple[str, ...]
    input_formats: tuple[str, ...]
    extraction_writers: tuple[str, ...]
    report_writers: tuple[str, ...]
    assistance_roles: tuple[str, ...]


def desktop_surface(edition_id: str) -> DesktopSurface:
    """Validate applicability and select recipes before any resource acquisition."""
    edition = editions.get_edition(edition_id)
    if "desktop" not in edition.interfaces:
        raise ValueError("Product does not support the desktop interface")
    selected = surface_rules.project_surface(edition.features,
        execution_preset=edition.execution_preset, report_style=edition.report_style)
    actions = tuple(dict.fromkeys((*('.'.join(path) for path in selected.commands),
                                   *(action for _, action in selected.menu_actions))))
    return DesktopSurface(actions, selected.providers, selected.input_formats,
        selected.extraction_writers, selected.report_writers, selected.assistance_roles)


@dataclass(frozen=True, repr=False)
class DesktopOverrides:
    """Caller-supplied writable location and process dependencies, never resolved here."""

    data_dir: Path
    environment: Mapping[str, str]
    clock: Callable[[], datetime]
    platform: object

    def __repr__(self) -> str:
        return "DesktopOverrides(<redacted>)"


@dataclass(frozen=True, repr=False)
class DesktopFactories:
    """Acquisition order is configuration, storage, provider, jobs, application.

    Each callable returns a context manager. The caller supplies implementations
    of the application, processor/job and provider interfaces it needs.
    """

    configuration: Callable[[DesktopOverrides], AbstractContextManager[object]]
    storage: Callable[[DesktopOverrides, object], AbstractContextManager[object]]
    provider: Callable[[DesktopOverrides, object, DesktopSurface], AbstractContextManager[object]]
    jobs: Callable[[DesktopOverrides, object, object], AbstractContextManager[object]]
    application: Callable[[DesktopOverrides, object, object, object, object, DesktopSurface], AbstractContextManager[object]]

    def __repr__(self) -> str:
        return "DesktopFactories()"


@dataclass(frozen=True)
class Diagnostic:
    stage: str
    code: str


@dataclass(repr=False)
class DesktopGraph:
    configuration: object
    storage: object
    provider: object
    jobs: object
    application: object
    _stack: ExitStack = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __repr__(self) -> str:
        return "DesktopGraph()"

    def close(self) -> Diagnostic | None:
        """Release all acquired contexts once, even when one cleanup fails."""
        if self._closed:
            return None
        self._closed = True
        try:
            self._stack.close()
        except Exception:
            return Diagnostic("shutdown", "shutdown_failed")
        return None


@dataclass(frozen=True, repr=False)
class StartupResult:
    graph: DesktopGraph | None = None
    diagnostic: Diagnostic | None = None
    cleanup_diagnostic: Diagnostic | None = None

    def __repr__(self) -> str:
        return f"StartupResult(diagnostic={self.diagnostic!r}, cleanup_diagnostic={self.cleanup_diagnostic!r})"


def start(factories: DesktopFactories, overrides: DesktopOverrides, *, edition: str) -> StartupResult:
    """Build a graph without widgets; failure contains only bounded diagnostic codes."""
    surface = desktop_surface(edition)
    stack = ExitStack()
    stage = "configuration"
    try:
        configuration = stack.enter_context(factories.configuration(overrides))
        stage = "storage"
        storage = stack.enter_context(factories.storage(overrides, configuration))
        stage = "provider"
        provider = stack.enter_context(factories.provider(overrides, configuration, surface))
        stage = "jobs"
        jobs = stack.enter_context(factories.jobs(overrides, storage, provider))
        stage = "application"
        application = stack.enter_context(factories.application(overrides, configuration, storage, provider, jobs, surface))
    except Exception:
        cleanup_diagnostic = None
        try:
            stack.close()
        except Exception:
            cleanup_diagnostic = Diagnostic("shutdown", "shutdown_failed")
        return StartupResult(diagnostic=Diagnostic(stage, "startup_failed"), cleanup_diagnostic=cleanup_diagnostic)
    return StartupResult(graph=DesktopGraph(configuration, storage, provider, jobs, application, stack))
