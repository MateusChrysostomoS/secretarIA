# syntax=docker/dockerfile:1
#
# Single image used by BOTH the API and the worker service on Easypanel.
# The two Easypanel services point at this same image with different start
# commands (see README.md -> Deploy).

FROM python:3.12-slim

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

EXPOSE 8000

# Default command = API service.
# The worker service on Easypanel OVERRIDES this start command with:
#   arq secretaria.workers.arq_worker.WorkerSettings
CMD ["uvicorn", "secretaria.main:app", "--host", "0.0.0.0", "--port", "8000"]
