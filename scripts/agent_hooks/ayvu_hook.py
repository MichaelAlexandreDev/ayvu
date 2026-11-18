#!/usr/bin/env python3
"""Agent hooks for Ayvu, shared by Claude Code and Codex.

Usage: ayvu_hook.py pretool|posttool|session < event.json

- pretool: denies actions that cannot be undone (an `--admin` merge, a forced push to main, destroying
  books, caches or private data, editing them directly), and asks before discarding local work or
  editing the quarantined v1 runtime.
- posttool: after a Python file is written, warns at once about the size limits and v1 imports that the
  fast gate would reject later. It never blocks.
- session: gives the agent the branch, the worktree state and the checks to run.

The script reads one JSON event on stdin and prints at most one JSON answer. It never fails the agent:
on any unexpected input it prints nothing and exits 0. Only the standard library is used.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


ADMIN_MERGE = re.compile(r"\bgh\s+pr\s+merge\b[^\n]*--admin\b")
FORCE_PUSH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b[^\n]*(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+[\w/-]+)")
MAIN_REF = re.compile(r"(?:^|[\s:+])(?:origin/)?(?:main|master)(?=\s|$)")
DISCARD_WORK = (
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?reset\b[^\n]*--hard\b"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?clean\b[^\n]*\s-[a-zA-Z]*f"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?checkout\s+--(?:\s|$)"),
)
DESTRUCTIVE_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|unlink|shred|truncate|mv|cp)\b"
    r"|\bsed\b[^\n]*\s-i(?:\s|$)|\bdd\b[^\n]*\bof=|(?:^|\s)>>?(?:\s|$)",
    re.MULTILINE,  # A command on a later line of a multi-line script is still a command.
)
SENSITIVE_IN_COMMAND = re.compile(
    r"(?:\.ayvu-pessoal[/\\]|(?:^|[\s'\"/])\.cache[/\\]|glossary\.json\b|[^\s'\"]+\.(?:epub|pdf|sqlite3?|db)\b)",
    re.IGNORECASE,
)
SENSITIVE_SUFFIXES = (".epub", ".pdf", ".sqlite", ".sqlite3", ".db")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
V1_IMPORT = re.compile(r"^\s*(?:from\s+(?:ayvu\.v1\b|\.+v1\b)|import\s+ayvu\.v1\b)", re.MULTILINE)


def repo_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def relative(raw: str, root: Path) -> str:
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def answer(event: str, **fields) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, **fields}}


def deny(reason: str) -> dict:
    return answer("PreToolUse", permissionDecision="deny", permissionDecisionReason=reason)


def ask(reason: str) -> dict:
    return answer("PreToolUse", permissionDecision="ask", permissionDecisionReason=reason)


HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
SHELLS = re.compile(r"(?:^|[|;&]\s*|\s)(?:sudo\s+)?(?:bash|sh|zsh|dash|ksh|eval)\b")


def without_heredoc_text(command: str) -> str:
    """Drops heredoc bodies that are only text for a non-shell command, such as notes, PR bodies or code.

    A body fed to a shell is code that runs, so it is kept and checked like the rest of the command.
    """
    lines, kept, index = command.split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC_START.finditer(line):
            if SHELLS.search(line[:match.start()]) or SHELLS.search(line[match.end():]):
                continue
            while index < len(lines) and lines[index].strip() != match.group(2):
                index += 1
            index += 1
    return "\n".join(kept)


COMMAND_SEPARATOR = re.compile(r"&&|\|\||[;|&\n]")


def simple_commands(command: str) -> list[str]:
    """Splits a chain at `&&`, `||`, `;`, `|`, `&` and newlines, so a flag belongs to its own command.

    Without this, `git push origin x && git worktree remove --force wt && git checkout main` read as a
    forced push to main.
    """
    return COMMAND_SEPARATOR.split(command)


def bash_decision(command: str) -> dict | None:
    command = without_heredoc_text(command)
    commands = simple_commands(command)
    if any(ADMIN_MERGE.search(part) for part in commands):
        return deny("Ayvu never merges with --admin. Update the branch, wait for CI and merge normally.")
    if any(FORCE_PUSH.search(part) and MAIN_REF.search(part) for part in commands):
        return deny("Force-pushing main is not allowed in Ayvu.")
    # Books and caches stay guarded across the whole chain: `cd .cache && rm x.sqlite` names them apart.
    if DESTRUCTIVE_SHELL.search(command) and SENSITIVE_IN_COMMAND.search(command):
        return deny("This would change or delete a book, a cache, a glossary or private data. "
                    "Confirm the exact target with the maintainer and use a recoverable flow.")
    if any(pattern.search(part) for part in commands for pattern in DISCARD_WORK):
        return ask("This discards local changes. Check the worktree has nothing to keep "
                   "(and that no suite, comparison or Docker run is reading it) before allowing it.")
    return None


def path_decision(path: str) -> dict | None:
    name = PurePosixPath(path).name.lower()
    lowered = path.lower()
    if lowered == ".ayvu-pessoal" or lowered.startswith(".ayvu-pessoal/"):
        return deny("`.ayvu-pessoal/` is the maintainer's private area; edit it only on an explicit request.")
    if name.endswith(SENSITIVE_SUFFIXES) or name == "glossary.json" or "/.cache/" in f"/{lowered}":
        return deny("Books, caches and glossaries are private data and are never edited directly.")
    if lowered.startswith("src/ayvu/v1/"):
        return ask("`src/ayvu/v1/` is the quarantined legacy runtime (D7): no new capability goes there. "
                   "Allow only a fix or a removal the maintainer asked for.")
    return None


def pretool(event: dict) -> dict | None:
    tool, data = event.get("tool_name"), event.get("tool_input") or {}
    if tool == "Bash" and isinstance(data.get("command"), str):
        return bash_decision(data["command"])
    if tool in EDIT_TOOLS:
        raw = data.get("file_path") or data.get("notebook_path")
        if isinstance(raw, str):
            return path_decision(relative(raw, repo_root()))
    return None


def ratchet_entries(root: Path) -> dict:
    try:
        return json.loads((root / "tests" / "size_ratchet.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ratchet_module(root: Path):
    """scripts/size_ratchet.py, so the hook measures exactly as the gate does."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ayvu_size_ratchet", root / "scripts" / "size_ratchet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def posttool(event: dict) -> dict | None:
    if event.get("tool_name") not in EDIT_TOOLS:
        return None
    data = event.get("tool_input") or {}
    raw = data.get("file_path") or data.get("notebook_path")
    if not isinstance(raw, str) or not raw.endswith(".py"):
        return None
    root = repo_root()
    rel = relative(raw, root)
    if not rel.startswith(("src/ayvu/", "tests/", "scripts/")):
        return None
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    ratchet, ledger, notes = ratchet_module(root), ratchet_entries(root), []
    lines = text.count("\n")
    limit = ledger.get("files", {}).get(rel, ratchet.FILE_LIMIT)
    if lines > limit:
        notes.append(f"{rel} has {lines} lines; its limit is {limit} (D10). Extract a cohesive piece "
                     "by responsibility, never squeeze lines.")
    try:
        functions = ratchet._functions(ast.parse(text))
    except SyntaxError:
        functions = []
    for name, size in functions:
        allowed = ledger.get("functions", {}).get(f"{rel}::{name}", ratchet.FUNCTION_LIMIT)
        if size > allowed:
            notes.append(f"`{name}` in {rel} has {size} lines; its limit is {allowed} (D10).")
    # D7: only composition imports v1; the layers contract covers every module below it.
    if rel.startswith("src/ayvu/") and not rel.startswith(("src/ayvu/v1/", "src/ayvu/composition/")) \
            and V1_IMPORT.search(text):
        notes.append(f"{rel} imports the quarantined v1 runtime; only composition may (D7).")
    if not notes:
        return None
    notes.append("Run scripts/check-fast.sh before finishing.")
    return answer("PostToolUse", additionalContext=" ".join(notes))


def session(_event: dict) -> dict | None:
    root = repo_root()
    def git(*args: str) -> str:
        try:
            out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=3)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    branch = git("branch", "--show-current") or "detached"
    changed = len([line for line in git("status", "--porcelain").splitlines() if line.strip()])
    context = (f"Ayvu workspace: branch `{branch}`, {changed} changed path(s). Follow AGENTS.md. Before "
               "writing code, decide its place (`ayvu-place-code`); after editing, run scripts/check-fast.sh; "
               "before pushing, run scripts/check.sh.")
    return answer("SessionStart", additionalContext=context)


HANDLERS = {"pretool": pretool, "posttool": posttool, "session": session}


def main(argv: list[str]) -> int:
    handler = HANDLERS.get(argv[1]) if len(argv) > 1 else None
    if handler is None:
        return 0
    try:
        event = json.load(sys.stdin)
        result = handler(event if isinstance(event, dict) else {})
    except Exception:  # A hook must never break the agent.
        return 0
    if result is not None:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
