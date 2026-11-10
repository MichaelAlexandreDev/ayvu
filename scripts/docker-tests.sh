#!/usr/bin/env bash
# Stage 2 of the test stages: run the suite on several Linux systems in Docker.
#
# Usage: scripts/docker-tests.sh [target...]
#
# Targets are <system>:<python>, where <python> is "system" (the distribution's own Python)
# or a uv-managed version (3.11 or 3.14), plus "constrained", which runs the core tests on
# one CPU and 1 GB of memory. With no target, every default target runs.
#
# Only files git tracks or would track are streamed into the container, so files that
# .gitignore excludes (books, caches, glossaries, private folders) never reach it. Tests run
# with no network, as an ordinary user, and the container is removed afterwards.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

declare -A BASES=(
  [ubuntu]="ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"
  [debian]="debian:trixie@sha256:9c6e381421d0f48d1b8133e573f33c185ee16cbf505eda9c4a8a4791b25ea8a6"
  [fedora]="fedora:43@sha256:f01112b9a1f1d0504ad962d9adfa401425033f8ab067e0a821904ebdde5b8ca5"
  [arch]="archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899"
)
declare -A FAMILIES=([ubuntu]=apt [debian]=apt [fedora]=dnf [arch]=pacman)
DEFAULT_TARGETS=(ubuntu:system ubuntu:3.11 ubuntu:3.14 debian:system fedora:system arch:system constrained)

targets=("$@")
[ ${#targets[@]} -gt 0 ] || targets=("${DEFAULT_TARGETS[@]}")

build() {
  local system="$1"
  echo "== building ayvu-test:$system" >&2
  docker build --quiet --tag "ayvu-test:$system" --file docker/Dockerfile \
    --build-arg "BASE=${BASES[$system]}" --build-arg "FAMILY=${FAMILIES[$system]}" . >/dev/null
}

source_stream() {
  git ls-files -z --cached --others --exclude-standard \
    | tar --null --files-from=- --ignore-failed-read --create --file=- 2>/dev/null
}

run() {
  local system="$1" python="$2"; shift 2
  echo "== $system, Python $python${*:+, $*}" >&2
  source_stream | docker run --rm --interactive --network none "${limits[@]}" \
    "ayvu-test:$system" ayvu-run-tests "$python" "$@"
}

declare -A built=()
failed=()
for target in "${targets[@]}"; do
  limits=()
  if [ "$target" = constrained ]; then
    system=ubuntu python=3.11 args=(-m core)
    limits=(--cpus 1 --memory 1g --memory-swap 1g)
  else
    system="${target%%:*}" python="${target#*:}" args=()
    if [ -z "${BASES[$system]:-}" ] || [ "$system" = "$target" ]; then
      echo "unknown target: $target (expected <ubuntu|debian|fedora|arch>:<system|3.11|3.14> or constrained)" >&2
      exit 2
    fi
  fi
  [ -n "${built[$system]:-}" ] || { build "$system"; built[$system]=1; }
  run "$system" "$python" "${args[@]}" || failed+=("$target")
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "all targets passed: ${targets[*]}" >&2
