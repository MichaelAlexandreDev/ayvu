#!/usr/bin/env python3
"""Architecture metrics for the refactoring (REF-01, #266).

Reports, for a source tree:
- files over 400, 800 and 1,000 lines;
- functions and methods over 80 lines;
- dependencies between module groups, and the pairs of groups that import each other.

Usage: scripts/arch_metrics.py [--root src/ayvu] [--json]

Grouping: a file inside a package belongs to its first package segment (``knowledge/cache/x.py`` is
``knowledge``). A top-level file is looked up in LEGACY_GROUPS, the grouping of the flat layout at
5796e49 into the modules of docs/architecture/modular-monolith-boundaries.md. As files move into
packages, their entries here stop being used and can be deleted.

The script reads source only: it never imports Ayvu, runs no code, and needs no network.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

FILE_THRESHOLDS = (400, 800, 1000)
FUNCTION_LIMIT = 80

PACKAGE_ALIASES = {"desktop": "interfaces"}

LEGACY_GROUPS = {
    "content": ("content_use_cases",),
    "formats": ("format_adapter", "format_service", "format_local", "epub_archive", "epub_format", "epub_io",
                "html_structure", "validation", "markdown_writer"),
    "providers": ("provider", "provider_capabilities", "provider_discovery", "provider_service", "provider_network",
                  "provider_session", "provider_libretranslate"),
    "translation": ("translation_plan", "translation_pipeline", "translation_engine", "translation_document",
                    "translation_context", "context_execution", "context_source", "chunking",
                    "translate_project", "translate_batch", "batch_output_policy", "preflight",
                    "resume_reuse"),
    "review": ("review_application", "review_exchange", "review_export", "review_import"),
    "interfaces": ("cli", "cli_progress", "cli_contracts", "desktop_bootstrap"),
}


@dataclass
class Report:
    files: int = 0
    lines: int = 0
    large_files: dict[int, list[tuple[str, int]]] = field(default_factory=dict)
    long_functions: list[tuple[str, int]] = field(default_factory=list)
    edges: dict[tuple[str, str], int] = field(default_factory=dict)
    ungrouped: list[str] = field(default_factory=list)

    @property
    def cycles(self) -> list[tuple[str, str]]:
        groups = sorted({group for edge in self.edges for group in edge})
        return [(a, b) for a, b in combinations(groups, 2) if (a, b) in self.edges and (b, a) in self.edges]

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "lines": self.lines,
            "large_files": {str(limit): [{"path": path, "lines": n} for path, n in items]
                            for limit, items in self.large_files.items()},
            "long_functions": [{"function": name, "lines": n} for name, n in self.long_functions],
            "edges": [{"from": a, "to": b, "imports": n} for (a, b), n in sorted(self.edges.items())],
            "cycles": [list(pair) for pair in self.cycles],
            "ungrouped": self.ungrouped,
        }


def module_name(root: Path, path: Path) -> tuple[str, ...]:
    parts = path.relative_to(root).with_suffix("").parts
    return parts[:-1] if parts[-1] == "__init__" else parts


def group_of(parts: tuple[str, ...], legacy: dict[str, str], packages: set[str]) -> str | None:
    """The group of a module, given as its path inside the package."""
    if not parts:
        return None
    if parts[0] in packages:
        return PACKAGE_ALIASES.get(parts[0], parts[0])
    return legacy.get(parts[0])


def imported_modules(tree: ast.AST, current: tuple[str, ...], is_package: bool, package: str) -> list[tuple[str, ...]]:
    """Every Ayvu module a file imports, as a path inside the package."""
    found = []
    base = current if is_package else current[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                if parts[0] == package:
                    found.append(parts[1:])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                target = anchor + (tuple(node.module.split(".")) if node.module else ())
            elif node.module and node.module.split(".")[0] == package:
                target = tuple(node.module.split(".")[1:])
            else:
                continue
            if target:
                found.append(target)
            else:
                found.extend((alias.name,) for alias in node.names)
    return [parts for parts in found if parts]


def measure(root: Path) -> Report:
    root = root.resolve()
    package = root.name
    legacy = {stem: group for group, stems in LEGACY_GROUPS.items() for stem in stems}
    packages = {path.name for path in root.iterdir() if path.is_dir() and (path / "__init__.py").is_file()}
    report = Report(large_files={limit: [] for limit in FILE_THRESHOLDS})
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        count = text.count("\n")
        report.files += 1
        report.lines += count
        for limit in FILE_THRESHOLDS:
            if count > limit:
                report.large_files[limit].append((relative, count))
        tree = ast.parse(text, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > FUNCTION_LIMIT:
                    report.long_functions.append((f"{relative}:{node.lineno}:{node.name}", length))
        parts = module_name(root, path)
        if not parts:
            continue
        source_group = group_of(parts, legacy, packages)
        if source_group is None:
            report.ungrouped.append(relative)
            continue
        for target in imported_modules(tree, parts, path.name == "__init__.py", package):
            target_group = group_of(target, legacy, packages)
            if target_group is None or target_group == source_group:
                continue
            key = (source_group, target_group)
            report.edges[key] = report.edges.get(key, 0) + 1
    report.long_functions.sort(key=lambda item: (-item[1], item[0]))
    for items in report.large_files.values():
        items.sort(key=lambda item: (-item[1], item[0]))
    return report


def render(report: Report) -> str:
    lines = [f"files: {report.files}  lines: {report.lines}"]
    for limit in FILE_THRESHOLDS:
        lines.append(f"files over {limit} lines: {len(report.large_files[limit])}")
    lines.append(f"functions over {FUNCTION_LIMIT} lines: {len(report.long_functions)}")
    lines.append("")
    lines.append(f"largest files (over {FILE_THRESHOLDS[0]} lines):")
    lines.extend(f"  {n:6} {path}" for path, n in report.large_files[FILE_THRESHOLDS[0]])
    lines.append("")
    lines.append(f"longest functions (over {FUNCTION_LIMIT} lines):")
    lines.extend(f"  {n:6} {name}" for name, n in report.long_functions)
    lines.append("")
    lines.append(f"module groups that import each other: {len(report.cycles)}")
    lines.extend(f"  {a} <-> {b}" for a, b in report.cycles)
    if report.ungrouped:
        lines.append("")
        lines.append("top-level files with no group (add them to LEGACY_GROUPS or move them into a package):")
        lines.extend(f"  {path}" for path in report.ungrouped)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "src" / "ayvu")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = measure(args.root)
    print(json.dumps(report.to_dict(), indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
