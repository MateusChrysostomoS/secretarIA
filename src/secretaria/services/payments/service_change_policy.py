"""The pure rule of a service change on an appointment with a PAID sinal.

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. No I/O, no clock,
no session: the caller measures `days_until` and passes the tenant's `min_days`
(`pix_difference_refund_min_days`). The orchestration (charge, ask, attention item) lives in
`service_change.py`.

    old deposit = what was paid (`PixDeposit.amount_cents`)
    new deposit = new price x the percentage that was PAID (`percent_applied`), rounded half up
    diff        = |new deposit - old deposit|   (always a magnitude, in cents)

- dearer, diff >= R$ 10  -> `increase_charge`: Pix of ONLY the difference, the service changes when
  it is paid; never blocked by `min_days` (the patient is paying, not being refunded).
- dearer, diff < R$ 10   -> `increase_free`: the change stands without a Pix (settled at the visit).
- cheaper, diff >= R$ 10 and days_until >= min_days -> `decrease_ask`: ask the patient whether they
  want the difference back (a yes only OPENS an attention item; nothing is returned automatically).
- cheaper otherwise      -> `decrease_keep`: the clinic keeps it (discounted on the rest of the
  visit).
- price unreadable / zero, or nothing was really paid -> `blocked` (nothing is saved).

TASK-085 R D-R12 (owner, 2026-11-18) revoked D-R3's "a convênio without sinal = new sinal 0": a
convênio never exempts the sinal, so the new deposit is always new price x percentage paid. Also
here, pure: `money_fields_changed`, THE predicate "did a
field that moves the sinal change?" (service, unit, doctor, convênio - D-R1/D-R3), shared by the
patient's edit (`appointment_edit_flow`, `appointment_edit_apply`) and the clinic's
(`staff_appointment_edit`). Contract: tasks/TASK-085/sinal-trocas/contrato.md.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

from secretaria.services.service_catalog import normalize as normalize_service_name

# The difference below which nobody is charged and nobody is asked. Fixed, not configurable.
MIN_DIFFERENCE_CENTS = 1000

Action = Literal[
    "no_change",
    "increase_charge",
    "increase_free",
    "decrease_ask",
    "decrease_keep",
    "blocked",
]


@dataclass(frozen=True)
class ChangeDecision:
    action: Action
    # Magnitude of the sinal difference in cents (0 for `no_change` and `blocked`).
    diff_cents: int
    # Only for `blocked`: a machine-readable cause the caller turns into a message.
    reason: str | None = None


def decide_service_change(
    *,
    paid_deposit_cents: int,
    percent_applied: int,
    new_price_cents: int | None,
    days_until: float,
    min_days: int,
) -> ChangeDecision:
    if (
        paid_deposit_cents <= 0
        or new_price_cents is None
        or new_price_cents <= 0
        or percent_applied <= 0
    ):
        return ChangeDecision(action="blocked", diff_cents=0, reason="price_unavailable")

    # Integer half-up rounding: Python's round() is banker's (50.5 -> 50), which would make the
    # cent depend on parity.
    new_deposit_cents = (new_price_cents * percent_applied + 50) // 100
    delta = new_deposit_cents - paid_deposit_cents

    if delta == 0:
        return ChangeDecision(action="no_change", diff_cents=0)
    if delta > 0:
        action: Action = "increase_charge" if delta >= MIN_DIFFERENCE_CENTS else "increase_free"
        return ChangeDecision(action=action, diff_cents=delta)

    diff = -delta
    asks = diff >= MIN_DIFFERENCE_CENTS and days_until >= min_days
    return ChangeDecision(action="decrease_ask" if asks else "decrease_keep", diff_cents=diff)


# --------------------------------------------------------------------------
# TASK-085 R: "did a money field change?" - one predicate for the patient and the clinic
# --------------------------------------------------------------------------

MONEY_FIELD_SERVICE = "service"
MONEY_FIELD_UNIT = "unit"
MONEY_FIELD_DOCTOR = "doctor"
MONEY_FIELD_INSURANCE = "insurance"
MONEY_FIELDS: tuple[str, ...] = (
    MONEY_FIELD_SERVICE,
    MONEY_FIELD_UNIT,
    MONEY_FIELD_DOCTOR,
    MONEY_FIELD_INSURANCE,
)

# R6 edit-dict key -> MoneyTerms attribute (the patient's edit and the engine's `pending_edit`).
_EDIT_KEYS = {
    "appointment_type": "service",
    "professional_id": "professional_id",
    "unit_id": "unit_id",
    "insurance": "insurance",
}


@dataclass(frozen=True)
class MoneyTerms:
    """The four fields of a consultation that can move its sinal (D-R1/D-R3). Plain values: ids
    as UUID or text (compared as text), names as written (compared like the catalog)."""

    service: str | None
    professional_id: UUID | str | None
    unit_id: UUID | str | None
    insurance: str | None

    @classmethod
    def of_appointment(cls, appointment: Any) -> "MoneyTerms":
        """From an `Appointment` (or anything with the same four attributes)."""
        return cls(
            service=getattr(appointment, "appointment_type", None),
            professional_id=getattr(appointment, "professional_id", None),
            unit_id=getattr(appointment, "unit_id", None),
            insurance=getattr(appointment, "insurance", None),
        )

    def with_edit(self, edit: Mapping[str, Any]) -> "MoneyTerms":
        """These terms after an R6 edit dict: a key ABSENT keeps the value; a key present (even
        with None) replaces it - the `pending_edit["unit_id"]` contract of TASK-085."""
        changes = {attr: edit[key] for key, attr in _EDIT_KEYS.items() if key in edit}
        return replace(self, **changes)


def _id_text(value: UUID | str | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text.lower() or None


def _insurance_text(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text.casefold() or None


def money_fields_changed(before: MoneyTerms, after: MoneyTerms) -> frozenset[str]:
    """Which of `MONEY_FIELDS` differ between two terms (empty = the sinal cannot move).

    Service names compare like the catalog (`service_catalog.normalize`: accents, case, spaces);
    ids as text (a UUID and its string are the same doctor/unit; "" is none); the convênio as
    trimmed, case-folded text ("" and None are both "not informed"). Whether a change really
    moves the price is the engine's job (`service_change.preview_service_change`): this only
    says that the engine must be asked."""
    changed: set[str] = set()
    if normalize_service_name(before.service) != normalize_service_name(after.service):
        changed.add(MONEY_FIELD_SERVICE)
    if _id_text(before.unit_id) != _id_text(after.unit_id):
        changed.add(MONEY_FIELD_UNIT)
    if _id_text(before.professional_id) != _id_text(after.professional_id):
        changed.add(MONEY_FIELD_DOCTOR)
    if _insurance_text(before.insurance) != _insurance_text(after.insurance):
        changed.add(MONEY_FIELD_INSURANCE)
    return frozenset(changed)
