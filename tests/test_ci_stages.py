"""The three test stages (#261): local check, Docker images, and a small core set on GitHub."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import CORE_FILES, CORE_TEST_FILES

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
CHECK = ROOT / "scripts" / "check.sh"
HOOK = ROOT / "scripts" / "hooks" / "pre-push"
DOCKER_TESTS = ROOT / "scripts" / "docker-tests.sh"
RELEASE_DOC = ROOT / "docs" / "release-workflow.md"

INVARIANTS = {
    "original_epub_unchanged", "zip_structure_and_mimetype", "visible_text_and_tags", "chunk_order_and_spacing",
    "cache_keys_and_scope", "resume", "glossary", "cli_entry_points", "local_only_network",
    "module_boundaries", "documentation_links", "ci_stages", "persisted_identities", "architecture_guardrails",
}
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def evaluate(expression: str, event_name: str, ref: str) -> bool:
    """Evaluates the small subset of GitHub expressions the workflow uses."""
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2]
    assert re.fullmatch(r"[\w.'()/*\s,=!|&-]+", body), body
    python = re.sub(r"github\.(event_name|ref)\b", r"ctx['\1']", body)
    python = python.replace("||", " or ").replace("&&", " and ").replace("startsWith(", "starts_with(")
    return bool(eval(python, {"__builtins__": {}}, {  # noqa: S307 - the input is the repository's own workflow
        "true": True, "false": False,
        "ctx": {"event_name": event_name, "ref": ref}, "starts_with": lambda value, prefix: value.startswith(prefix),
    }))


EVENTS = {
    "pull_request": ("pull_request", "refs/pull/7/merge"),
    "push_main": ("push", "refs/heads/main"),
    "release_tag": ("push", "refs/tags/v0.1.0"),
    "manual": ("workflow_dispatch", "refs/heads/main"),
}


def jobs_for(event: str) -> set[str]:
    name, ref = EVENTS[event]
    return {job_id for job_id, job in workflow()["jobs"].items() if evaluate(job.get("if", "true"), name, ref)}


def run_steps(job: dict, event: str) -> list[str]:
    name, ref = EVENTS[event]
    return [step["run"] for step in job["steps"] if "run" in step and evaluate(step.get("if", "true"), name, ref)]


def test_the_workflow_triggers_on_pull_requests_main_release_tags_and_manual_runs():
    triggers = workflow()[True]  # PyYAML reads the bare key `on` as a boolean
    assert triggers["push"] == {"branches": ["main"], "tags": ["v*"]}
    assert "pull_request" in triggers and "workflow_dispatch" in triggers


@pytest.mark.parametrize("event,expected", [
    ("pull_request", {"ci-gate"}), ("push_main", {"ci-gate"}),
    ("release_tag", {"full-matrix"}), ("manual", {"full-matrix"}),
])
def test_each_event_selects_the_intended_jobs(event, expected):
    assert jobs_for(event) == expected


def test_ci_gate_is_one_job_on_the_oldest_python_with_no_matrix():
    job = workflow()["jobs"]["ci-gate"]
    assert job["name"] == "ci-gate" and "strategy" not in job and "needs" not in job
    setup = next(step for step in job["steps"] if step.get("name") == "Set up Python")
    assert setup["with"]["python-version"] == "3.11"


def test_pull_requests_run_the_core_tests_and_main_runs_the_full_suite():
    job = workflow()["jobs"]["ci-gate"]
    pull_request = run_steps(job, "pull_request")
    main = run_steps(job, "push_main")
    assert "uv run --no-sync pytest -m core" in pull_request
    assert "uv run --no-sync pytest" not in pull_request
    assert "uv run --no-sync pytest" in main and "uv run --no-sync pytest -m core" not in main
    for steps in (pull_request, main):
        assert "uv lock --check" in steps
        assert any("uv build" in step for step in steps)
        assert any("ayvu-smoke/bin/ayvu\" --help" in step for step in steps)


def test_manual_runs_and_release_tags_cover_python_3_11_and_3_14_with_the_full_suite():
    job = workflow()["jobs"]["full-matrix"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]
    steps = run_steps(job, "manual")
    assert "uv run --no-sync pytest" in steps and any("uv build" in step for step in steps)


def test_every_action_is_pinned_to_a_full_commit():
    uses = [step["uses"] for job in workflow()["jobs"].values() for step in job["steps"] if "uses" in step]
    assert uses and all(PINNED_ACTION.match(value) for value in uses), uses


def test_the_workflow_keeps_read_only_permissions_and_timeouts():
    data = workflow()
    assert data["permissions"] == {"contents": "read"}
    assert all(job["timeout-minutes"] <= 15 for job in data["jobs"].values())


def test_the_core_map_names_every_invariant_with_existing_test_files():
    assert set(CORE_TEST_FILES) == INVARIANTS
    for invariant, names in CORE_TEST_FILES.items():
        assert names, invariant
        for name in names:
            assert (ROOT / "tests" / name).is_file(), (invariant, name)
    assert CORE_FILES == {name for names in CORE_TEST_FILES.values() for name in names}


def test_the_core_marker_is_registered():
    import tomllib

    markers = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("core:") for marker in markers)


def test_the_core_selection_collects_tests_only_from_the_core_files():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "core", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    node_ids = [line for line in result.stdout.splitlines() if "::" in line]
    files = {Path(node_id.split("::", 1)[0]).relative_to("tests").as_posix() for node_id in node_ids}
    assert files == CORE_FILES
    # The full suite has over 5,000 tests; the core set must stay a small share of it.
    assert 500 < len(node_ids) < 2500


def test_dependabot_groups_version_updates_and_leaves_security_updates_separate():
    updates = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    assert {update["package-ecosystem"] for update in updates} == {"uv", "github-actions"}
    for update in updates:
        groups = list(update["groups"].values())
        assert groups == [{"applies-to": "version-updates", "patterns": ["*"]}]


PRIVATE_PATTERNS = [
    ".ayvu-*/", "**/*.epub", "**/*.pdf", "**/*.sqlite", "**/*.sqlite3", "**/.cache/", "**/glossary.json",
    ".venv/", ".codex", "**/*AGENTS*.md", "**/CLAUDE.md", "**/GEMINI*.md", ".claude/",
]


def dockerignore_lines() -> list[str]:
    return [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_dockerignore_excludes_everything_and_re_includes_only_the_dependency_files():
    lines = dockerignore_lines()
    assert lines[0] == "*"
    assert {line[1:] for line in lines if line.startswith("!")} == {"pyproject.toml", "uv.lock", "docker/run-tests.sh"}


@pytest.mark.parametrize("pattern", PRIVATE_PATTERNS)
def test_dockerignore_also_names_each_private_pattern(pattern):
    assert pattern in dockerignore_lines()


def test_the_docker_script_streams_only_files_git_would_track_and_runs_with_no_network():
    text = DOCKER_TESTS.read_text(encoding="utf-8")
    assert "git ls-files -z --cached --others --exclude-standard" in text
    assert "--network none" in text
    assert "--cpus 1 --memory 1g" in text
    for target in ("ubuntu:system", "ubuntu:3.11", "ubuntu:3.14", "debian:system", "fedora:system", "arch:system"):
        assert target in text
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER ayvu" in dockerfile
    assert re.search(r"^FROM ghcr\.io/astral-sh/uv:[\d.]+@sha256:[0-9a-f]{64}", dockerfile, re.M)
    assert all(re.search(r"@sha256:[0-9a-f]{64}\"$", line) for line in text.splitlines() if re.match(r"\s+\[\w+\]=\"\w+:", line))


def test_the_local_check_runs_every_stage_one_step_and_the_hook_calls_it():
    text = CHECK.read_text(encoding="utf-8")
    for command in ("uv lock --check", "uv run --no-sync pytest -q", "GITHUB_ACTIONS=true uv run --no-sync pytest -q",
                    "git diff --check"):
        assert command in text
    assert "set -euo pipefail" in text
    assert "scripts/check.sh" in HOOK.read_text(encoding="utf-8")
    for script in (CHECK, HOOK, DOCKER_TESTS, ROOT / "docker" / "run-tests.sh"):
        assert script.stat().st_mode & 0o111, script


def test_the_release_workflow_documents_the_three_stages_and_the_unprotected_main():
    text = RELEASE_DOC.read_text(encoding="utf-8")
    for needle in ("scripts/check.sh", "scripts/docker-tests.sh", "-m core", "CORE_TEST_FILES", "2.000 minutos",
                   "--no-verify"):
        assert needle in text, needle
    assert "exige a pull\n  request atualizada" not in text
