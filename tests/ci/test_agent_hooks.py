"""The shared agent hooks deny what cannot be undone, ask before discarding work, and warn early."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.support.paths import PROJECT_ROOT as ROOT

HOOK = ROOT / "scripts" / "agent_hooks" / "ayvu_hook.py"


def run(mode, event, project=ROOT):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    result = subprocess.run([sys.executable, str(HOOK), mode], input=json.dumps(event), capture_output=True,
                            text=True, env=env, timeout=30, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None


def decision(event):
    answer = run("pretool", event)
    return "allow" if answer is None else answer["permissionDecision"]


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def edit(path, tool="Edit"):
    return {"tool_name": tool, "tool_input": {"file_path": path}}


@pytest.mark.parametrize("command", [
    "gh pr merge 371 --squash --admin",
    "git push --force origin main",
    "git push origin +main",
    "git push --force-with-lease origin HEAD:main",
    "rm -rf .cache/translations.sqlite",
    "cp other.json glossary.json",
    "rm book.epub",
    "sed -i s/a/b/ .ayvu-pessoal/notes.md",
])
def test_actions_that_cannot_be_undone_are_denied(command):
    assert decision(bash(command)) == "deny"


@pytest.mark.parametrize("command", ["git reset --hard HEAD", "git -C ../wt clean -fd", "git checkout -- ."])
def test_discarding_local_work_asks_first(command):
    assert decision(bash(command)) == "ask"


@pytest.mark.parametrize("command", [
    "git push -u origin feat/x",
    "git push -f origin feat/main-x",
    "gh pr merge 371 --merge",
    "uv run pytest -q",
    "rm -rf build/",
    "cat book.epub.md",
])
def test_ordinary_commands_pass(command):
    assert decision(bash(command)) == "allow"


@pytest.mark.parametrize("path", [
    "book.epub", "notes/report.pdf", ".cache/x.sqlite", "glossary.json", ".ayvu-pessoal/docs/map.md",
])
def test_private_data_is_never_edited_directly(path):
    assert decision(edit(path, "Write")) == "deny"


def test_editing_the_quarantined_v1_runtime_asks_first():
    assert decision(edit(str(ROOT / "src" / "ayvu" / "v1" / "epub.py"))) == "ask"


def test_editing_ordinary_code_passes():
    assert decision(edit(str(ROOT / "src" / "ayvu" / "translation" / "project.py"))) == "allow"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "size_ratchet.py", tmp_path / "scripts" / "size_ratchet.py")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "size_ratchet.json").write_text(json.dumps(
        {"files": {"src/ayvu/kernel/listed.py": 450}, "functions": {}}), encoding="utf-8")
    (tmp_path / "src" / "ayvu" / "kernel").mkdir(parents=True)
    (tmp_path / "src" / "ayvu" / "composition").mkdir(parents=True)
    return tmp_path


def write(project, relative, text):
    path = project / relative
    path.write_text(text, encoding="utf-8")
    return run("posttool", edit(str(path), "Write"), project)


def test_a_file_or_function_over_its_limit_is_reported_after_writing(project):
    body = "def big():\n" + "    x = 1\n" * 85 + "y = 2\n" * 320
    answer = write(project, "src/ayvu/kernel/big.py", body)
    context = answer["additionalContext"]
    assert "has 406 lines; its limit is 400" in context and "`big` in src/ayvu/kernel/big.py has 86 lines" in context


def test_a_listed_file_is_held_to_its_ratchet_entry(project):
    assert write(project, "src/ayvu/kernel/listed.py", "x = 1\n" * 440) is None
    assert "its limit is 450" in write(project, "src/ayvu/kernel/listed.py", "x = 1\n" * 460)["additionalContext"]


def test_a_v1_import_outside_composition_is_reported(project):
    assert "only composition may (D7)" in write(project, "src/ayvu/kernel/uses.py", "from ayvu.v1 import cache\n")["additionalContext"]
    assert write(project, "src/ayvu/composition/uses.py", "from ..v1 import cache\n") is None


def test_small_files_and_other_files_say_nothing(project):
    assert write(project, "src/ayvu/kernel/small.py", "x = 1\n") is None
    (project / "README.md").write_text("x\n" * 900, encoding="utf-8")
    assert run("posttool", edit(str(project / "README.md"), "Write"), project) is None


def test_the_session_context_names_the_branch_and_the_checks():
    context = run("session", {})["additionalContext"]
    assert "Follow AGENTS.md" in context and "scripts/check-fast.sh" in context


def test_bad_input_never_breaks_the_agent():
    for mode in ("pretool", "posttool", "session", "unknown"):
        result = subprocess.run([sys.executable, str(HOOK), mode], input="not json", capture_output=True,
                                text=True, timeout=30)
        assert result.returncode == 0 and result.stdout == ""
