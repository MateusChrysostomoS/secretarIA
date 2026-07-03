"""human_backup_24_7 plugin: hand inbound messages to a human outside business hours.

Entitlement-gated addon (`human_backup_24_7`). Wires an `on_inbound` hook
(see `plugins/base.py:InboundContext` and `registry.run_on_inbound`), invoked
from workers/tasks.py:_send_bot_reply AFTER the entitlement gate and BEFORE
the deterministic flow router / LLM.

When an inbound message arrives OUTSIDE the tenant's configured business
hours, the hook:
  1. flips the conversation to HUMAN_ACTIVE (via HandoverManager) so the bot
     stays quiet on it going forward, same as a human-secretary echo would;
  2. sends ONE ack text to the patient, on the tenant's own WhatsApp number;
  3. best-effort emails the tenant's contact address (fail-open — a failed
     alert email never blocks the ack or the handover); and
  4. returns True, so `_send_bot_reply` sends nothing else this turn.

Inside business hours (or when the tenant has no business_hours configured
at all — "empty config -> never outside") the hook returns False and the
normal bot flow proceeds untouched.
"""

from datetime import UTC, datetime, time as dtime
from zoneinfo import ZoneInfo

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation
from secretaria.plugins.base import InboundContext, PluginSpec
from secretaria.plugins.registry import register
from secretaria.services.email import send_human_backup_alert
from secretaria.services.handover import HandoverManager
from secretaria.services.tenant_config import active_business_hours
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# date.weekday(): Mon=0 -> business_hours JSON key. Mirrors
# services/calendar.py's _WEEKDAY_KEYS exactly (same JSON shape).
_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_hhmm(value: object) -> dtime | None:
    try:
        hour, minute = str(value).split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def _is_outside_business_hours(tenant, now_utc: datetime | None = None) -> bool:
    """True when `now` falls outside every configured window for today.

    Empty/unconfigured `business_hours` (no day carries so much as one
    window) -> False (never outside): a tenant that hasn't set hours yet gets
    the normal bot flow, not silent handovers. This is deliberately distinct
    from a tenant that HAS configured hours but is specifically closed today
    (an empty list for today, with other days non-empty) — that legitimately
    means "outside", so it is read from the RAW `business_hours`, not the
    `active_business_hours` view (which drops empty-list days entirely and
    would make the two cases indistinguishable).
    """
    if not active_business_hours(tenant):
        return False  # nothing configured anywhere -> never outside

    now_utc = now_utc or datetime.now(UTC)
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        tz = UTC
    local = now_utc.astimezone(tz)

    windows = (tenant.business_hours or {}).get(_WEEKDAY_KEYS[local.weekday()]) or []
    if not windows:
        return True  # hours ARE configured (elsewhere), but closed all day today

    current = local.time()
    for window in windows:
        start = _parse_hhmm(window.get("start"))
        end = _parse_hhmm(window.get("end"))
        if start is not None and end is not None and start <= current <= end:
            return False
    return True


async def _on_inbound(ctx: InboundContext) -> bool:
    tenant = ctx.tenant
    try:
        if not _is_outside_business_hours(tenant):
            return False
    except Exception as exc:
        logger.warning("human_backup_hours_check_failed", error=str(exc), tenant_id=str(tenant.id))
        return False

    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, ctx.conversation_id)
                if conversation is None:
                    return False
                await HandoverManager(session).set_human_active(conversation)
    except Exception as exc:
        logger.error(
            "human_backup_handover_failed",
            error=str(exc),
            conversation_id=str(ctx.conversation_id),
        )
        return False

    settings = get_settings()
    client = WhatsAppClient.for_tenant(tenant, ctx.waba_token)
    try:
        await client.send_text_message(to=ctx.patient_wa_id, body=settings.HUMAN_BACKUP_ACK_TEXT)
    except Exception as exc:
        # The handover already happened (a human will see the conversation in
        # the app regardless) - a failed ack send is logged, not fatal.
        logger.error(
            "human_backup_ack_send_failed",
            error=str(exc),
            conversation_id=str(ctx.conversation_id),
        )

    logger.info(
        "human_backup_engaged", conversation_id=str(ctx.conversation_id), tenant_id=str(tenant.id)
    )

    if tenant.contact_email:
        try:
            await send_human_backup_alert(tenant.contact_email, tenant.clinic_name)
        except Exception as exc:
            logger.warning("human_backup_alert_email_failed", error=str(exc))

    return True


register(
    PluginSpec(
        id="human_backup_24_7",
        entitlement_keys=("human_backup_24_7",),
        on_inbound=_on_inbound,
    )
)
