# Test image for stage 2 of the test stages (see docs/release-workflow.md).
#
# The build context holds only pyproject.toml, uv.lock and docker/run-tests.sh (see .dockerignore).
# The source to test arrives at run time as a tar stream on stdin, so no project file is baked in.
# scripts/docker-tests.sh passes BASE and FAMILY; the defaults build the Ubuntu image.
ARG BASE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv

FROM ${BASE}
ARG FAMILY=apt

# git is needed by tests that read the repository; tar unpacks the source stream.
RUN set -eu; \
    case "${FAMILY}" in \
      apt) apt-get update \
           && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
              python3 git ca-certificates tar \
           && rm -rf /var/lib/apt/lists/* ;; \
      dnf) dnf install -y python3 git tar shadow-utils && dnf clean all ;; \
      pacman) pacman -Syu --noconfirm --needed python git tar && pacman -Scc --noconfirm ;; \
      *) echo "unknown FAMILY: ${FAMILY}" >&2; exit 2 ;; \
    esac; \
    useradd --create-home --uid 1000 --user-group ayvu 2>/dev/null \
      || useradd --create-home --uid 1001 --user-group ayvu

COPY --from=uv /uv /uvx /usr/local/bin/
COPY docker/run-tests.sh /usr/local/bin/ayvu-run-tests

# Tests run as an ordinary user, because permission tests are meaningless as root.
USER ayvu
ENV HOME=/home/ayvu UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=manual
WORKDIR /home/ayvu/deps
COPY --chown=ayvu:ayvu pyproject.toml uv.lock ./

# Install dependencies for every interpreter this image tests: the system Python and uv's 3.11 and 3.14.
# A stub package lets uv build the editable project once, so the build backend is cached for
# the offline sync at run time.
RUN set -eu; \
    uv python install 3.11 3.14; \
    mkdir -p src/ayvu && : > src/ayvu/__init__.py && : > README.md; \
    for py in system 3.11 3.14; do \
      if [ "$py" = system ]; then interpreter=/usr/bin/python3; else interpreter="$py"; fi; \
      UV_PROJECT_ENVIRONMENT="/home/ayvu/venvs/$py" uv sync --extra dev --locked --python "$interpreter"; \
    done; \
    rm -rf src README.md
