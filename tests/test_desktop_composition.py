"""Desktop composition is injectable, reversible, and independent of the host."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ayvu.desktop.bootstrap.composition import DesktopFactories, DesktopOverrides, start


STAGES = ("configuration", "storage", "provider", "jobs", "application")


def setup_factories(events: list[tuple], fail: str | None = None, cleanup_fail: str | None = None):
    resources = {stage: object() for stage in STAGES}

    def factory(stage):
        @contextmanager
        def create(*args):
            events.append(("enter", stage, *args))
            if stage == fail:
                raise RuntimeError("private details")
            try:
                yield resources[stage]
            finally:
                events.append(("exit", stage))
                if stage == cleanup_fail:
                    raise RuntimeError("private cleanup details")
        return create

    return DesktopFactories(**{stage: factory(stage) for stage in STAGES}), resources


def test_start_injects_resources_and_closes_in_reverse_order(tmp_path: Path) -> None:
    events = []
    factories, resources = setup_factories(events)
    overrides = DesktopOverrides(data_dir=tmp_path, environment={}, clock=lambda: 123, platform=object())
    result = start(factories, overrides)

    assert result.diagnostic is None
    graph = result.graph
    assert graph is not None
    for stage in STAGES:
        assert getattr(graph, stage) is resources[stage]
    assert events == [
        ("enter", "configuration", overrides),
        ("enter", "storage", overrides, resources["configuration"]),
        ("enter", "provider", overrides, resources["configuration"]),
        ("enter", "jobs", overrides, resources["storage"], resources["provider"]),
        ("enter", "application", overrides, resources["configuration"], resources["storage"], resources["provider"], resources["jobs"]),
    ]
    graph.close()
    graph.close()
    assert events[5:] == [("exit", stage) for stage in reversed(STAGES)]


@pytest.mark.parametrize("failed", STAGES)
def test_failed_start_rolls_back_acquired_resources(tmp_path: Path, failed: str) -> None:
    events = []
    factories, _ = setup_factories(events, failed)
    overrides = DesktopOverrides(data_dir=tmp_path, environment={}, clock=lambda: 123, platform=object())
    result = start(factories, overrides)

    assert result.graph is None
    assert result.diagnostic is not None
    assert result.diagnostic.stage == failed
    assert result.diagnostic.code == "startup_failed"
    assert "private details" not in str(result.diagnostic)
    index = STAGES.index(failed)
    assert [event[1] for event in events if event[0] == "enter"] == list(STAGES[:index + 1])
    assert [event[1] for event in events if event[0] == "exit"] == list(reversed(STAGES[:index]))



def test_shutdown_continues_after_cleanup_failure(tmp_path: Path) -> None:
    events = []
    factories, _ = setup_factories(events, cleanup_fail="provider")
    overrides = DesktopOverrides(data_dir=tmp_path, environment={}, clock=lambda: 123, platform=object())
    graph = start(factories, overrides).graph
    assert graph is not None
    diagnostic = graph.close()
    assert diagnostic is not None
    assert (diagnostic.stage, diagnostic.code) == ("shutdown", "shutdown_failed")
    assert "private cleanup details" not in str(diagnostic)
    assert [event[1] for event in events if event[0] == "exit"] == list(reversed(STAGES))
    assert graph.close() is None
    assert len([event for event in events if event[0] == "exit"]) == len(STAGES)


def test_rollback_continues_after_cleanup_failure(tmp_path: Path) -> None:
    events = []
    factories, _ = setup_factories(events, fail="application", cleanup_fail="provider")
    overrides = DesktopOverrides(data_dir=tmp_path, environment={}, clock=lambda: 123, platform=object())
    result = start(factories, overrides)
    assert result.graph is None
    assert (result.diagnostic.stage, result.diagnostic.code) == ("application", "startup_failed")
    assert (result.cleanup_diagnostic.stage, result.cleanup_diagnostic.code) == ("shutdown", "shutdown_failed")
    assert "private" not in repr(result)
    assert [event[1] for event in events if event[0] == "exit"] == list(reversed(STAGES[:-1]))

def test_desktop_imports_have_no_host_side_effects(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key not in {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", r"""
import importlib, sys, types

def audit(event, args):
    if event == "open":
        mode = args[1]
        flags = args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & 0x243):
            raise AssertionError("filesystem write on import")
    if event.startswith("socket.") or event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        raise AssertionError("host side effect on import: " + event)

class NoWidget:
    def __init__(self, *args, **kwargs):
        raise AssertionError("widget constructed on import")
qt = types.ModuleType("PySide6")
qt.__path__ = []
widgets = types.ModuleType("PySide6.QtWidgets")
widgets.QApplication = widgets.QWidget = NoWidget
qt.QtWidgets = widgets
sys.modules.update({"PySide6": qt, "PySide6.QtWidgets": widgets})
sys.addaudithook(audit)
for name in ("ayvu.desktop", "ayvu.desktop.bootstrap", "ayvu.desktop.bootstrap.composition",
             "ayvu.desktop.presentation", "ayvu.desktop.presentation.views", "ayvu.desktop.viewmodels",
             "ayvu.desktop.adapters", "ayvu.desktop.adapters.qt", "ayvu.desktop.resources", "ayvu.desktop.platform"):
    importlib.import_module(name)
"""],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
