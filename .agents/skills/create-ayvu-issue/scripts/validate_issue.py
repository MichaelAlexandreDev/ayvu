#!/usr/bin/env python3
"""Validate the structural quality of one locally drafted Ayvu issue."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_SECTIONS = (
    "Context",
    "Objective",
    "Scope",
    "Out of scope",
    "Technical requirements",
    "Acceptance criteria",
    "Tests required",
    "Dependencies",
    "Suggested branch",
    "Priority",
    "Estimate",
    "Recommended model",
    "Labels",
)

BRANCH_RE = re.compile(
    r"`(?:feat|fix|refactor|test|docs|research|build|ci)/[a-z0-9]+(?:-[a-z0-9]+)*`"
)
CHECKBOX_RE = re.compile(r"^- \[ \] .+", re.MULTILINE)
DEPENDENCY_RE = re.compile(r"\[(?:[A-Z][A-Z0-9]*-\d+|#[0-9]+)\]")
PLACEHOLDERS = (
    "TODO",
    "TBD",
    "[SERIES-NN]",
    "[DESKTOP-XXX]",
    "<issue>",
    "Current model",
    "Current verified model",
)
TITLE_RE = re.compile(r"(?m)^# \[([A-Z][A-Z0-9]*-\d+)\] ([A-Z][^\n]+)$")
CODEX_MODELS = {
    "GPT-6.1 Sol": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "GPT-5.6 Sol": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "GPT-5.6 Terra": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "GPT-5.6 Luna": {"low", "medium", "high", "xhigh", "max"},
}
CODEX_MODEL_RE = re.compile(
    r"(?m)^- Codex: `([^`]+)` \(`([^`]+)`\)$"
)


def section_body(text: str, title: str) -> str:
    match = re.search(rf"(?m)^## {re.escape(title)}\s*$", text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"(?m)^## .+$", text[start:])
    end = len(text) if not next_heading else start + next_heading.start()
    return text[start:end]


def validate(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []

    title_matches = TITLE_RE.findall(text)
    if len(title_matches) != 1:
        errors.append("expected exactly one canonical # [SERIES-NN] title")
        issue_id = None
    else:
        issue_id = title_matches[0][0]

    positions = []
    for section in REQUIRED_SECTIONS:
        marker = f"## {section}"
        matches = list(re.finditer(rf"(?m)^{re.escape(marker)}\s*$", text))
        count = len(matches)
        if count != 1:
            errors.append(f"expected exactly one {marker!r}, found {count}")
            continue
        positions.append(matches[0].start())
        if not section_body(text, section).strip():
            errors.append(f"section {section!r} is empty")

    if positions and positions != sorted(positions):
        errors.append("required sections are out of canonical order")

    acceptance = section_body(text, "Acceptance criteria")
    if len(CHECKBOX_RE.findall(acceptance)) < 3:
        errors.append("acceptance criteria need at least three unchecked checkboxes")

    tests = section_body(text, "Tests required").lower()
    if "test" not in tests:
        errors.append("Tests required does not name test behavior")

    branch = section_body(text, "Suggested branch")
    if not BRANCH_RE.search(branch):
        errors.append("Suggested branch is missing or not type/kebab-case")

    priority = section_body(text, "Priority")
    priority_match = re.search(r"`(P[0-3]) — (?:Blocking|High|Medium|Low)`", priority)
    if not priority_match:
        errors.append("Priority must use the canonical P0-P3 form")

    estimate = section_body(text, "Estimate")
    if not re.search(r"`(?:XS|S|M|L)`", estimate):
        errors.append("Estimate must be XS, S, M, or L; split XL work")

    for placeholder in PLACEHOLDERS:
        if placeholder in text:
            errors.append(f"unresolved placeholder: {placeholder}")

    dependencies = section_body(text, "Dependencies")
    if "None." not in dependencies and not DEPENDENCY_RE.search(dependencies):
        errors.append("Dependencies must be None or contain explicit identifiers")
    if issue_id:
        series, current_number = issue_id.rsplit("-", 1)[0], int(issue_id.rsplit("-", 1)[1])
        for dependency in re.findall(rf"\[({re.escape(series)}-\d+)\]", dependencies):
            dependency_number = int(dependency.rsplit("-", 1)[1])
            if dependency_number >= current_number:
                errors.append(f"dependency must use an earlier local ID: {dependency}")

    model = section_body(text, "Recommended model")
    model_matches = CODEX_MODEL_RE.findall(model)
    if len(model_matches) != 1:
        errors.append("Recommended model must contain exactly one Codex model and effort")
    else:
        model_name, effort = model_matches[0]
        allowed_efforts = CODEX_MODELS.get(model_name)
        if allowed_efforts is None:
            errors.append("Recommended model must use a currently verified Codex model")
        elif effort not in allowed_efforts:
            errors.append(
                f"reasoning effort {effort!r} is not available for {model_name}"
            )
    unexpected_model_lines = re.findall(
        r"(?m)^- (?!Codex:|Why:)[^\n]+$", model
    )
    if unexpected_model_lines:
        errors.append("Recommended model must not include non-Codex recommendations")
    if not re.search(r"(?m)^- Why: .+", model):
        errors.append("Recommended model must include a risk-based reason")

    labels = section_body(text, "Labels")
    if priority_match:
        expected_label = f"`priority:{priority_match.group(1).lower()}`"
        if expected_label not in labels:
            errors.append(f"Labels must include {expected_label}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue", type=Path)
    args = parser.parse_args()
    if not args.issue.is_file():
        print(f"ERROR: file not found: {args.issue}", file=sys.stderr)
        return 2

    errors = validate(args.issue)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {args.issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
