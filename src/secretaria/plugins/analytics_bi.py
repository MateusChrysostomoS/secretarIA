"""analytics_bi plugin: record a minimal booking event (post_booking).

Entitlement-gated addon: the hook runs for a tenant entitled to EITHER
`analytics_bi` (basic summary) OR `analytics_bi_advanced` (advanced dashboard),
so an advanced-only tenant still gets its rows recorded. Every hook run records
exactly ONE `AnalyticsEvent` row (event_type="appointment_booked") per booked
appointment, with a deliberately minimal, non-personal payload —
`appointment_type`, `professional_id` (str or None) and `source`
("agent"|"flow") — see models/analytics_event.py's docstring for why. The
doctor-hub summary endpoint (api/hub/analytics.py) aggregates these rows.
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models.analytics_event import AnalyticsEvent
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register

logger = get_logger(__name__)

_BOOKED_EVENT = "appointment_booked"


async def _post_booking(ctx: PostBookingContext) -> None:
    try:
        async with async_session_factory() as session:
            async with session.begin():
                session.add(
                    AnalyticsEvent(
                        tenant_id=ctx.tenant.id,
                        event_type=_BOOKED_EVENT,
                        payload={
                            "appointment_type": ctx.appointment.appointment_type,
                            "professional_id": (
                                str(ctx.appointment.professional_id)
                                if ctx.appointment.professional_id
                                else None
                            ),
                            "source": ctx.source,
                        },
                    )
                )
    except Exception as exc:
        logger.warning(
            "analytics_event_record_failed", error=str(exc), tenant_id=str(ctx.tenant.id)
        )
        return
    logger.info(
        "analytics_event_recorded",
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
    )


# entitlement_keys is ANY-OF: record booking events for a tenant entitled to EITHER the
# basic `analytics_bi` summary OR the `analytics_bi_advanced` dashboard, so an
# advanced-only tenant still accumulates the rows both views read (an advanced dashboard
# over an empty table would otherwise be permanently blank). Same multi-key shape as
# reminders.py's ("bronze_1", "reactivation_pack").
ANALYTICS_BI_SPEC = PluginSpec(
    id="analytics_bi",
    entitlement_keys=("analytics_bi", "analytics_bi_advanced"),
    post_booking=_post_booking,
)
register(ANALYTICS_BI_SPEC)
