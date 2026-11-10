#!/bin/sh
# Runs inside the stage 2 test image. Reads the project source as a tar stream on stdin,
# syncs it offline into the venv prepared for the chosen interpreter, and runs pytest.
#
# Usage: ayvu-run-tests <system|3.11|3.14> [pytest arguments...]
set -eu

interpreter="${1:?usage: ayvu-run-tests <system|3.11|3.14> [pytest arguments...]}"
shift

work="$(mktemp -d)"
cd "$work"
tar -xf -

export UV_PROJECT_ENVIRONMENT="$HOME/venvs/$interpreter" UV_OFFLINE=1
# Every test isolates its own folders too; these keep anything that slips through inside the container.
export XDG_CONFIG_HOME="$work/.xdg-config" XDG_CACHE_HOME="$HOME/.cache" XDG_DATA_HOME="$work/.xdg-data"

uv sync --extra dev --locked --offline --quiet
"$UV_PROJECT_ENVIRONMENT/bin/python" -c 'import sys; print("Python", sys.version.split()[0])'
exec uv run --no-sync pytest -q -p no:cacheprovider "$@"
