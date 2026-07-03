"""FastAPI application entrypoint.

Run with:
    uvicorn secretaria.main:app --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from secretaria.api import health, internal, internal_privacy, webhook
from secretaria.api.admin import panel, tenants
from secretaria.api.hub import (
    analytics,
    calendar,
    config,
    conversations,
    oauth,
    professionals,
    units,
)
from secretaria.config import get_settings
from secretaria.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the arq Redis pool on startup, close it on shutdown.

    The pool is stored on `app.state.arq_pool` so the webhook handler can
    enqueue jobs. If Redis is unreachable the API still starts (so /health
    works); POST /webhook then returns 503 until the queue is back.
    """
    settings = get_settings()
    logger.info("api_starting", env=settings.APP_ENV)
    try:
        app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        logger.info("api_arq_pool_connected")
    except Exception as exc:
        app.state.arq_pool = None
        logger.error("api_arq_pool_connect_failed", error=str(exc))

    try:
        yield
    finally:
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            # redis>=5 renamed close() -> aclose(); support both.
            closer = getattr(pool, "aclose", None) or pool.close
            await closer()
        logger.info("api_stopped")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="SecretarIA WhatsApp Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    # CORS for the Next.js doctor portal (origins are configurable per env).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router, tags=["health"])
    app.include_router(webhook.router, tags=["webhook"])
    # Internal-only service-to-service surface (brain-api -> appointments/patients),
    # guarded by X-Internal-Api-Key at the router level (self-tags "internal").
    app.include_router(internal.router)
    # LGPD privacy endpoints (export/erase), same X-Internal-Api-Key gate.
    app.include_router(internal_privacy.router)
    app.include_router(panel.router, tags=["admin"])
    # Admin fleet view: list tenants + per-tenant Google Calendar health (router self-tags).
    app.include_router(tenants.router)
    # Doctor hub: tenant config + Google Calendar OAuth onboarding + calendar actions.
    app.include_router(config.router)
    app.include_router(oauth.router)
    app.include_router(calendar.router)
    # Addon CRUD: multi_professional / multi_unit (entitlement + limit gated
    # in the routers themselves; see api/hub/professionals.py, api/hub/units.py).
    app.include_router(professionals.router)
    app.include_router(units.router)
    # Per-conversation manual handover control for the dashboard (list +
    # BOT_ACTIVE/HUMAN_ACTIVE toggle); see api/hub/conversations.py.
    app.include_router(conversations.router)
    # analytics_bi addon: booking-count summary (entitlement gated in the router).
    app.include_router(analytics.router)
    return app


app = create_app()
