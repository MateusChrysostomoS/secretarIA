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
"""

from dataclasses import dataclass
from typing import Literal

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
        new_price_cents is None
        or new_price_cents <= 0
        or paid_deposit_cents <= 0
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
