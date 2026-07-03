"""pix_whatsapp plugin: ask for a Pix deposit right after a booking (post_booking).

Entitlement-gated addon (`pix_whatsapp`). `Tenant.pix_key` is the clinic's
own Pix key — a public payment address (like an email/phone/random key used
to receive Pix transfers), NOT a secret credential; still, only `tenant_id`
(never the key or the rendered message body) is ever logged, out of caution.

Hook: `pix_key` missing -> silent no-op (a tenant with the addon on but no
key configured yet gets no message, not a broken one). A booking with no
associated patient (e.g. a block slot created via the doctor hub) has no one
to send to -> silent no-op too. Otherwise renders a deposit-request message
(services/payments/pix.py — a STUB; see that module's docstring for what
"real" looks like) and sends it to the patient via
`WhatsAppClient.for_tenant`.
"""

from secretaria.core.logging import get_logger
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.payments.pix import create_deposit_charge
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)


async def _post_booking(ctx: PostBookingContext) -> None:
    if not ctx.tenant.pix_key:
        logger.debug("pix_charge_skipped_no_key", tenant_id=str(ctx.tenant.id))
        return
    if ctx.patient is None:
        logger.debug("pix_charge_skipped_no_patient", tenant_id=str(ctx.tenant.id))
        return

    charge = create_deposit_charge(ctx.tenant, ctx.appointment)
    client = WhatsAppClient.for_tenant(ctx.tenant, ctx.waba_token)
    try:
        await client.send_text_message(to=ctx.patient.wa_id, body=charge.message)
    except Exception as exc:
        logger.warning("pix_charge_send_failed", error=str(exc), tenant_id=str(ctx.tenant.id))
        return

    logger.info(
        "pix_charge_sent", tenant_id=str(ctx.tenant.id), appointment_id=str(ctx.appointment.id)
    )


PIX_WHATSAPP_SPEC = PluginSpec(
    id="pix_whatsapp", entitlement_keys=("pix_whatsapp",), post_booking=_post_booking
)
register(PIX_WHATSAPP_SPEC)
