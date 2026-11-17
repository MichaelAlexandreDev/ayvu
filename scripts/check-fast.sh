#!/usr/bin/env bash
# Fast architecture gate: the checks an agent or a contributor runs after every edit session.
#
# Usage: scripts/check-fast.sh
#
# Runs the import contracts, the size ratchet, the architecture tests and the whitespace check, in
# about 20 seconds. It does not replace scripts/check.sh (the full suite in both modes) before a push.
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*" >&2; }

step "lint-imports"
uv run --no-sync lint-imports

step "size ratchet"
python3 scripts/size_ratchet.py

step "architecture tests"
uv run --no-sync pytest -q -p no:cacheprovider tests/architecture

step "git diff --check"
git diff --check
git diff --cached --check

printf '\nfast gate passed\n' >&2
