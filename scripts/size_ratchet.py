#!/usr/bin/env python3
"""Size ratchet for D10 of docs/architecture/decisions/package-structure.md (REF-03, #271).

Files may have at most 400 lines and functions at most 80, tests included. Files and functions that were
larger when the ratchet started are listed in tests/size_ratchet.json with their size. A listed entry may
only shrink: tests/test_size_ratchet.py fails when it grows, and also when it shrinks without the entry
being lowered, so the list always matches the code.

Usage:
  scripts/size_ratchet.py            report what differs from tests/size_ratchet.json
  scripts/size_ratchet.py --update   lower shrunk entries and drop entries back within the limit
  scripts/size_ratchet.py --move OLD NEW
                                     carry the entries of a file moved from OLD to NEW (with git mv)

--update never raises an entry and never adds one. Growth has to be undone in the code. --move renames
entries and keeps their sizes, so a moved file is held to the size it had.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests" / "size_ratchet.json"
SCOPES = ("src/ayvu", "tests", "scripts")
FILE_LIMIT = 400
FUNCTION_LIMIT = 80


def python_files(root: Path = ROOT) -> list[Path]:
    files = []
    for scope in SCOPES:
        files.extend(path for path in (root / scope).rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _functions(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                found.append((name, child.end_lineno - child.lineno + 1))
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def measure(root: Path = ROOT) -> dict[str, dict[str, int]]:
    """Every file over FILE_LIMIT lines and every function over FUNCTION_LIMIT lines."""
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = text.count("\n")
        if lines > FILE_LIMIT:
            files[relative] = lines
        for name, length in _functions(ast.parse(text, filename=relative)):
            if length > FUNCTION_LIMIT:
                key = f"{relative}::{name}"
                functions[key] = max(length, functions.get(key, 0))
    return {"files": dict(sorted(files.items())), "functions": dict(sorted(functions.items()))}


def load(path: Path = LEDGER) -> dict[str, dict[str, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"files": data["files"], "functions": data["functions"]}


def differences(ledger: dict[str, dict[str, int]], actual: dict[str, dict[str, int]]) -> list[str]:
    problems = []
    for kind, limit in (("files", FILE_LIMIT), ("functions", FUNCTION_LIMIT)):
        listed, current = ledger[kind], actual[kind]
        for name, size in current.items():
            if name not in listed:
                problems.append(f"new {kind[:-1]} over the {limit}-line limit: {name} has {size} lines; split it (D10)")
            elif size > listed[name]:
                problems.append(f"{name} grew from {listed[name]} to {size} lines; listed entries may only shrink (D10)")
            elif size < listed[name]:
                problems.append(f"{name} shrank from {listed[name]} to {size} lines; run scripts/size_ratchet.py --update")
        for name in listed.keys() - current.keys():
            problems.append(f"{name} is now within {limit} lines or gone; run scripts/size_ratchet.py --update")
    return sorted(problems)


def update(ledger: dict[str, dict[str, int]], actual: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Lower and drop entries only. Anything that grew or is new keeps failing."""
    result = {}
    for kind in ("files", "functions"):
        listed, current = ledger[kind], actual[kind]
        result[kind] = {name: min(size, current[name]) for name, size in listed.items() if name in current}
    return result


def move(ledger: dict[str, dict[str, int]], old: str, new: str) -> dict[str, dict[str, int]]:
    """Renames the file entry OLD and its function entries to NEW, keeping every size."""
    if any(name == new or name.startswith(new + "::") for entries in ledger.values() for name in entries):
        raise ValueError(f"{new} is already in the ledger")

    def renamed(name: str) -> str:
        return new + name[len(old):] if name == old or name.startswith(old + "::") else name

    return {kind: {renamed(name): size for name, size in entries.items()} for kind, entries in ledger.items()}


def write(ledger: dict[str, dict[str, int]], path: Path = LEDGER) -> None:
    payload = {
        "_comment": "D10 size ratchet (REF-03, #271). Entries may only shrink; see scripts/size_ratchet.py.",
        "files": dict(sorted(ledger["files"].items())),
        "functions": dict(sorted(ledger["functions"].items())),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower shrunk entries and drop finished ones")
    parser.add_argument("--move", nargs=2, action="append", default=[], metavar=("OLD", "NEW"),
                        help="carry a moved file's entries to its new path")
    args = parser.parse_args(argv)
    actual = measure()
    ledger = load()
    for old, new in args.move:
        ledger = move(ledger, old, new)
    if args.move and not args.update:
        write(ledger)
    if args.update:
        ledger = update(ledger, actual)
        write(ledger)
    problems = differences(ledger, actual)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
