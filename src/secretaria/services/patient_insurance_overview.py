"""What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-10-12).

Feeds `GET /internal/brain-message/insurance-overview`, which brain-api calls on behalf of a
patient session (the clinic is the session's, never the patient's input). The plans come from the
SAME list the booking flow offers (`load_tenant_insurance`, mode-aware). Only the clinic's OWN note
on an "Outro" plan goes out - catalog metadata is internal and is already `None` there.

TASK-085 R U1b (owner, 2026-11-18): the sheet exists to show the clinic's notes, so ONLY the plans
WITH a note are listed (a plan without one is just its name, already in the convênio list). The
Portal offers the sheet only when the convênio card says a note exists
(`flow_router.insurance_plans_have_clinic_notes`, the same test over the same list).

TASK-085 R D-R12 (owner, 2026-11-18): a convenio never exempts the sinal, so there is no per-plan
deposit any more - the sinal is the clinic's one rule (`deposit`), the same for every convenio and
for "particular". The per-plan payment text ("Varia por plano...", "Pagamento: confirme...") was
removed with it (owner: "TIRE ISSO").

Never returned: row or catalog ids, doctors (names or ids), `accepted_by`, anything about a
patient. The caller is internal-key only. Read-only: nothing here writes or moves money.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Tenant
from secretaria.services.insurance_catalog import load_tenant_insurance


async def patient_insurance_overview(session: AsyncSession, tenant_id: UUID) -> dict | None:
    """The patient-facing view of one clinic's convenios, or None for an unknown clinic."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    deposit_on = bool(tenant.pix_deposit_enabled)
    insurance = await load_tenant_insurance(session, tenant_id)
    plans: list[dict] = []
    for plan in insurance.plans:
        name = str(plan.get("name") or "").strip()
        note = plan.get("note")
        note = note.strip() if isinstance(note, str) else ""
        if name and note:
            plans.append({"name": name, "clinic_note": note})
    return {
        "deposit": {
            "enabled": deposit_on,
            "percent": tenant.pix_deposit_percent if deposit_on else None,
        },
        "plans": plans,
    }
