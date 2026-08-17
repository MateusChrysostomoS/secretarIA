# syntax=docker/dockerfile:1
#
# Single image used by BOTH the API and the worker service on Easypanel.
# The two Easypanel services point at this same image with different start
# commands (see README.md -> Deploy).

FROM python:3.12-slim

# git - needed by `uv sync` to fetch the transcription-core dependency, pinned to a tag
# via a git source in pyproject.toml (python:3.12-slim has no git binary by default).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# uv - the Python package manager.
# TODO(prod): pin uv to an explicit version tag instead of :latest.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1. Install dependencies only (cached layer - rebuilt only when deps change).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 2. Copy the application source and install the project itself.
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

# 3. Build identity (FIX_01 §5.1). Deliberately the LAST layer before CMD: a
# new SHA must not invalidate the dependency or install layers above.
#
# Passed by the build, e.g.
#   docker build --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
#                --build-arg BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) .
# `.git` is NOT in this image and must never be read at runtime. A builder
# that passes neither (Easypanel's plain Dockerfile build does not) is still
# fine: `source_fingerprint` in core/build_info.py hashes the shipped sources,
# so API/worker parity stays provable with no pipeline support at all. Setting
# these as runtime env vars in the deploy panel also works — same Settings.
ARG BUILD_SHA=""
ARG BUILT_AT=""
ENV BUILD_SHA=${BUILD_SHA} \
    BUILT_AT=${BUILT_AT}

EXPOSE 8000

# Default command = API service.
# The worker service on Easypanel OVERRIDES this start command with:
#   arq secretaria.workers.arq_worker.WorkerSettings
CMD ["uvicorn", "secretaria.main:app", "--host", "0.0.0.0", "--port", "8000"]
