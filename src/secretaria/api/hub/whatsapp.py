"""Hub routes to connect the clinic's WhatsApp from the secretarIA backend (TASK-093 Plano 3).

Stage a: built, NOT switched on. While `WHATSAPP_CONNECT_BACKEND_ENABLED` is False (default)
every route below answers 404 - before authentication - and the current brain-api path stays
the only way to connect. There is no Portal screen yet; these routes are what it will call.

    GET  /tenants/me/whatsapp/embedded-signup-config  manager  {app_id, config_id}
    POST /tenants/me/whatsapp/attempts                manager  connect (services/whatsapp_connect)
    GET  /tenants/me/whatsapp/connection              any hub user  masked state, never a secret

The WhatsApp CHANNEL stays locked (services/operating_channel.py::RELEASED_OPERATING_CHANNELS):
connecting a number does not make the secretary answer on it; `channel_released` only reports
the switch.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant, require_clinic_manager
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.tenant import OPERATING_CHANNEL_WHATSAPP
from secretaria.models.whatsapp_connection_attempt import ATTEMPT_FAILED
from secretaria.schemas.whatsapp_connect import (
    EmbeddedSignupConfigOut,
    WhatsAppAttemptIn,
    WhatsAppAttemptOut,
    WhatsAppConnectionOut,
)
from secretaria.services import operating_channel, whatsapp_connect
from secretaria.services.whatsapp_connect import (
    CODE_EXCHANGE_FAILED,
    PHONE_NUMBER_CONFLICT,
    WABA_SUBSCRIBE_FAILED,
    ConnectError,
)

logger = get_logger(__name__)


def _require_backend_enabled() -> None:
    """The whole router is invisible (404) until the owner turns the flag on."""
    if not get_settings().WHATSAPP_CONNECT_BACKEND_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


router = APIRouter(
    prefix="/tenants/me/whatsapp",
    tags=["hub-whatsapp"],
    dependencies=[Depends(_require_backend_enabled)],
)

# Plain words for the clinic (shown as is by the future Portal screen).
_FAILURES: dict[str, tuple[int, str]] = {
    PHONE_NUMBER_CONFLICT: (
        status.HTTP_409_CONFLICT,
        "Este número de WhatsApp já está ligado a outra clínica.",
    ),
    CODE_EXCHANGE_FAILED: (
        status.HTTP_502_BAD_GATEWAY,
        "Não foi possível confirmar a conexão com a Meta. Tente conectar de novo.",
    ),
    WABA_SUBSCRIBE_FAILED: (
        status.HTTP_502_BAD_GATEWAY,
        "A Meta não liberou o acesso à conta do WhatsApp. Tente conectar de novo.",
    ),
}


def _failure(code: str) -> HTTPException:
    status_code, message = _FAILURES.get(code, _FAILURES[CODE_EXCHANGE_FAILED])
    return HTTPException(status_code, detail={"code": code, "message": message})


def _mask(phone_number_id: str) -> str:
    return "••••" + phone_number_id[-4:]


@router.get("/embedded-signup-config", response_model=EmbeddedSignupConfigOut)
async def get_embedded_signup_config(
    _tenant: Tenant = Depends(require_clinic_manager),
) -> EmbeddedSignupConfigOut:
    """The Meta app id and Embedded Signup configuration id (public values; never the secret)."""
    settings = get_settings()
    return EmbeddedSignupConfigOut(
        app_id=settings.META_APP_ID or None,
        config_id=settings.META_ES_CONFIG_ID or None,
    )


@router.post("/attempts", response_model=WhatsAppAttemptOut)
async def post_attempt(
    body: WhatsAppAttemptIn,
    tenant: Tenant = Depends(require_clinic_manager),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppAttemptOut:
    """Connect the clinic's WhatsApp with what the Meta popup returned. Manager only.

    Idempotent on `attempt_id`: a replay never calls Meta again and answers the same.
    409 `phone_number_conflict` (number of another clinic); 502 `code_exchange_failed` /
    `waba_subscribe_failed` (Meta refused or was unreachable). A failed attempt is recorded and
    no credential is written.
    """
    tenant_id = tenant.id
    try:
        attempt = await whatsapp_connect.complete_attempt(
            session,
            tenant_id,
            body.attempt_id,
            code=body.code,
            phone_number_id=body.phone_number_id,
            waba_id=body.waba_id,
        )
        await session.commit()
    except ConnectError as err:
        await session.commit()  # records the failed attempt; no credential was written
        raise _failure(err.code) from None
    except IntegrityError:
        # Another clinic committed the same number between our check and our commit (the
        # partial unique index on tenants.phone_number_id): nothing of ours was kept.
        await session.rollback()
        attempt = await whatsapp_connect.start_attempt(session, tenant_id, body.attempt_id)
        attempt.status = ATTEMPT_FAILED
        attempt.failure_code = PHONE_NUMBER_CONFLICT
        await session.commit()
        logger.info(
            "whatsapp_connect_attempt_failed",
            tenant_id=str(tenant_id),
            attempt_id=body.attempt_id,
            failure_code=PHONE_NUMBER_CONFLICT,
        )
        raise _failure(PHONE_NUMBER_CONFLICT) from None
    return WhatsAppAttemptOut(
        attempt_id=attempt.attempt_id, status=attempt.status, failure_code=attempt.failure_code
    )


@router.get("/connection", response_model=WhatsAppConnectionOut)
async def get_connection(tenant: Tenant = Depends(get_current_tenant)) -> WhatsAppConnectionOut:
    """Whether the clinic's WhatsApp is connected (by either path), masked; never a secret."""
    connected = tenant.phone_number_id is not None
    connected_at = tenant.connected_at
    if connected_at is not None and connected_at.tzinfo is None:
        connected_at = connected_at.replace(tzinfo=UTC)
    return WhatsAppConnectionOut(
        connected=connected,
        phone_number_id_masked=_mask(tenant.phone_number_id) if connected else None,
        connected_at=connected_at if connected else None,
        channel_released=OPERATING_CHANNEL_WHATSAPP
        in operating_channel.RELEASED_OPERATING_CHANNELS,
    )
