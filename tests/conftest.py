"""Shared pytest fixtures and deterministic test environment."""

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

# Configure a deterministic environment BEFORE any `secretaria` import, so that
# Settings() picks these values up. Real env vars take precedence over `.env`.
os.environ["APP_ENV"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["META_APP_SECRET"] = "test-app-secret"
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
os.environ["META_ACCESS_TOKEN"] = "test-access-token"
os.environ["META_PHONE_NUMBER_ID"] = "1234567890"
# Valid Fernet key (test-only) so the encrypted-credential seams work in tests.
os.environ["ENCRYPTION_KEY"] = "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w="
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://secretaria:secretaria@localhost:5432/secretaria",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An httpx AsyncClient bound to the FastAPI app via ASGITransport.

    The app lifespan does NOT run under ASGITransport, so these tests need no
    Redis or Postgres connection.
    """
    from secretaria.config import get_settings

    get_settings.cache_clear()

    from secretaria.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
