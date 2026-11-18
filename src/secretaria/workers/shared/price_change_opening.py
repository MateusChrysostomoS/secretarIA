"""The first message of a conversation opened from a "the price changed" e-mail (TASK-085 R, D-R14).

3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §2.3, §4.5). The e-mail links to the Portal with
`lembrete=<appointment_reminders.id>` of a `price_change` row; `workers/portal/open.py`
(`_brain_message_entry_decision` -> `process_brain_message_enter`) recognizes the row's kind and
calls `send_price_change_opening` instead of the usual opening, even with reminders v2 switched
off. This module opens a session, calls `price_change.open_price_notice` (which checks the row is
this patient's and this clinic's, marks it answered, and decides with the price in force NOW -
it commits), then delivers `price_change.price_opening_messages(...)` after the commit on the
patient's channel. Opening the link twice never charges twice (kind "pending").

Channel-neutral (`workers/shared/`): never imports `whatsapp/` or `portal/`. U0 (contract slice)
froze the signature; UB2 implements it. Logs: ids and outcome kinds only.
"""

from uuid import UUID

from secretaria.models import Tenant

_UB2 = "TASK-085 R UB2"


async def send_price_change_opening(
    reply, tenant: Tenant, *, reminder_id: UUID, redis=None
) -> bool | None:
    """Send the price-change opening for the reply context's patient. True when it was sent,
    False when it no longer applies (row not this patient's/clinic's, consultation gone or
    suspended: the caller falls back to the usual opening), None when superseded (the
    conversation moved on before the write: nothing sent)."""
    raise NotImplementedError(_UB2)
