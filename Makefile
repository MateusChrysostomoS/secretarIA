# SecretarIA - developer shortcuts.
# All targets run through `uv`, so no manual venv activation is needed.

.PHONY: help install dev worker migrate makemigration test lint format seed up down logs

help:
	@echo "install        - sync dependencies into .venv"
	@echo "up / down      - start / stop local Postgres + Redis (docker compose)"
	@echo "migrate        - apply database migrations (alembic upgrade head)"
	@echo "makemigration  - autogenerate a migration: make makemigration m=\"message\""
	@echo "dev            - run the API with autoreload"
	@echo "worker         - run the arq background worker"
	@echo "seed           - create a development tenant"
	@echo "test           - run the test suite"
	@echo "lint           - run ruff checks (lint + format check)"
	@echo "format         - auto-format and auto-fix with ruff"

install:
	uv sync

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	uv run alembic upgrade head

makemigration:
	uv run alembic revision --autogenerate -m "$(m)"

dev:
	uv run uvicorn secretaria.main:app --reload --host 0.0.0.0 --port 8000

worker:
	uv run arq secretaria.workers.arq_worker.WorkerSettings

seed:
	uv run python scripts/seed_dev.py

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .
