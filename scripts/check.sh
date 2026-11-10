#!/usr/bin/env bash
# Stage 1 of the test stages: the full local check, run before every push.
#
# Usage: scripts/check.sh
#
# Runs the lock check, the full suite twice (plainly and as GitHub Actions sees it, since some
# tests change behaviour under GITHUB_ACTIONS) and the whitespace check. Exits non-zero on the
# first failure.
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*" >&2; }

step "uv lock --check"
uv lock --check

step "uv sync --extra dev --locked"
uv sync --extra dev --locked --quiet

step "pytest"
uv run --no-sync pytest -q

step "pytest with GITHUB_ACTIONS=true"
GITHUB_ACTIONS=true uv run --no-sync pytest -q

step "git diff --check"
git diff --check
git diff --cached --check

printf '\nstage 1 passed\n' >&2
