"""Pix deposit-charge message rendering — STUB, not a payment integration.

`create_deposit_charge` renders a pt-BR WhatsApp message asking the patient
for a deposit to confirm a freshly booked appointment, embedding the
tenant's own Pix key (`Tenant.pix_key`) and the appointment's details. That
message IS the `pix_whatsapp` addon's entire behavior today — there is no
real charge, no QR code, no "copia e cola" payload, nothing a Pix app could
actually pay.

TODO: real PSP integration (a Pix-enabled payment gateway — Mercado Pago,
Asaas, Stripe Pix, ...) generating an ACTUAL charge and a webhook that flips
the appointment out of SCHEDULED once the deposit lands. Until that webhook
exists, a booked appointment stays SCHEDULED regardless of whether the
patient ever pays the deposit — this stub only ever sends the ask, it never
observes or requires an answer, and nothing in the booking flow is gated on
payment.
"""

from dataclasses import dataclass
from datetime import UTC
from zoneinfo import ZoneInfo

from secretaria.models import Appointment, Tenant


@dataclass(frozen=True)
class PixCharge:
    """A rendered (not real) deposit-request message."""

    code: str
    message: str


def _price_for_appointment(tenant: Tenant, appointment: Appointment) -> str | None:
    """Best-effort price lookup from the matching appointment_type entry, by name."""
    if not appointment.appointment_type:
        return None
    for entry in tenant.appointment_types or []:
        if entry.get("name") == appointment.appointment_type:
            price = entry.get("price")
            if price:
                return str(price)
    return None


def _when_text(tenant: Tenant, appointment: Appointment) -> str:
    if appointment.start_at is None:
        return "horário a confirmar"
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        tz = UTC
    start = appointment.start_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.astimezone(tz).strftime("%d/%m/%Y às %H:%M")


def create_deposit_charge(tenant: Tenant, appointment: Appointment) -> PixCharge:
    """Render a pt-BR deposit-request message for a freshly booked appointment.

    `tenant.pix_key` is embedded verbatim — a Pix key is a public payment
    address (see models/tenant.py's docstring), safe to put in a WhatsApp
    message. Amount wording uses the matching appointment_type's price when
    one is configured; otherwise falls back to generic "garantir sua vaga"
    wording rather than inventing a number.
    """
    appt_type = appointment.appointment_type or "sua consulta"
    when = _when_text(tenant, appointment)
    price = _price_for_appointment(tenant, appointment)
    amount_line = f"no valor de {price}" if price else "para garantir sua vaga"

    message = (
        f"Para confirmar {appt_type} na {tenant.clinic_name}, marcada para {when}, "
        f"pedimos um depósito {amount_line} via Pix, na chave: {tenant.pix_key}.\n"
        "Assim que o pagamento for identificado, sua consulta estará garantida."
    )
    return PixCharge(code=f"pix-stub-{appointment.id}", message=message)
