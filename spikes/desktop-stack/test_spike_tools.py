"""Checks for research tooling, independently of production Ayvu code."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import pytest


SPIKE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, SPIKE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_environment_drops_inherited_loader_and_proxy_settings(tmp_path, monkeypatch):
    runner = load("benchmark_startup")
    for key in ("LD_LIBRARY_PATH", "TCL_LIBRARY", "TK_LIBRARY", "PYTHONPATH", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "untrusted-inherited-value")
    environment = runner._child_environment(tmp_path)
    for key in ("LD_LIBRARY_PATH", "TCL_LIBRARY", "TK_LIBRARY", "PYTHONPATH", "HTTPS_PROXY"):
        assert key not in environment
    assert environment["HOME"] == str(tmp_path)


def test_benchmark_runtime_is_explicit_and_does_not_merge_inherited_paths(tmp_path, monkeypatch):
    runner = load("benchmark_startup")
    monkeypatch.setenv("LD_LIBRARY_PATH", "untrusted-inherited-value")
    runtime = tmp_path / "approved-runtime"
    environment = runner._child_environment(tmp_path, runtime)
    assert environment["LD_LIBRARY_PATH"] == str(runtime / "usr/lib")
    assert environment["TCL_LIBRARY"] == str(runtime / "usr/lib/tcl8.6")
    assert environment["TK_LIBRARY"] == str(runtime / "usr/lib/tk8.6")


@pytest.mark.parametrize("candidate", ["qt", "wx"])
def test_runtime_option_rejects_other_candidates_before_launch(candidate, tmp_path):
    result = subprocess.run(
        [sys.executable, str(SPIKE / "benchmark_startup.py"), candidate,
         "--tk-runtime-root", str(tmp_path)],
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 2
    assert "only supported for Tk on Linux" in result.stderr


def test_runtime_option_reports_missing_directory_without_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SPIKE / "benchmark_startup.py"), "tk",
         "--tk-runtime-root", str(tmp_path / "missing")],
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_bundle_measurement_rejects_absolute_and_escaping_symlinks(tmp_path):
    scanner = load("measure_bundle")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    absolute = artifact / "absolute"
    absolute.symlink_to(tmp_path / "outside")
    with pytest.raises(SystemExit, match="absolute symlinks"):
        scanner._safe_link_bytes(absolute, artifact)
    escaping = artifact / "escaping"
    escaping.symlink_to("../outside")
    with pytest.raises(SystemExit, match="escape"):
        scanner._safe_link_bytes(escaping, artifact)


def test_bundle_measurement_counts_relative_link_text_without_following(tmp_path):
    scanner = load("measure_bundle")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    link = artifact / "link"
    link.symlink_to("missing-library.so")
    assert scanner._safe_link_bytes(link, artifact) == 18


@pytest.mark.parametrize(
    "document",
    [SPIKE.parents[1] / "docs/architecture/adr-desktop-stack.md",
     *SPIKE.glob("*.md"), *(SPIKE / "results").glob("*.md")],
    ids=lambda path: path.name,
)
def test_research_document_local_links_resolve(document):
    project = SPIKE.parents[1].resolve()
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        parsed = urlsplit(target.strip())
        if parsed.scheme or not parsed.path:
            continue
        path = (document.parent / unquote(parsed.path)).resolve()
        assert path.is_relative_to(project)
        assert path.is_file(), f"broken local link in {document.name}: {target}"
