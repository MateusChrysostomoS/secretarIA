"""What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-10-12).

Feeds `GET /internal/brain-message/insurance-overview`, which brain-api calls on behalf of a
patient session (the clinic is the session's, never the patient's input). The list is the SAME
one the booking flow offers (`load_tenant_insurance`, mode-aware) with the SAME fixed payment
text the list row shows (`insurance_payment_description`). Only the clinic's OWN note on an
"Outro" plan goes out - catalog metadata is internal and is already `None` there.

The deposit rule mirrors `deposit_lifecycle._insurance_charges_deposit`: only an explicit
`charge_deposit = False` skips the deposit. In independent mode the same convenio can be set
differently by each active doctor; then the answer is "varies", without saying who.

Never returned: row or catalog ids, doctors (names or ids), `accepted_by`, anything about a
patient. The caller is internal-key only. Read-only: nothing here writes or moves money.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, ProfessionalInsurancePlan, Tenant, TenantInsurancePlan
from secretaria.services.insurance_catalog import _independent_plan_id, load_tenant_insurance
from secretaria.services.insurance_payment_copy import insurance_payment_description

DEPOSIT_CHARGED = "charged"
DEPOSIT_NOT_CHARGED = "not_charged"
DEPOSIT_VARIES = "varies"


async def _charge_flags(
    session: AsyncSession, tenant_id: UUID, mode: str | None
) -> dict[str, set[bool]]:
    """Plan id (the same id space as `load_tenant_insurance`'s plans) -> the deposit flags seen."""
    if mode == "independent":
        rows = await session.execute(
            select(
                ProfessionalInsurancePlan.catalog_id,
                ProfessionalInsurancePlan.id,
                ProfessionalInsurancePlan.charge_deposit,
            )
            .join(Professional, Professional.id == ProfessionalInsurancePlan.professional_id)
            .where(
                ProfessionalInsurancePlan.tenant_id == tenant_id,
                ProfessionalInsurancePlan.tenant_plan_id.is_(None),
                Professional.is_active.is_(True),
            )
        )
        flags: dict[str, set[bool]] = {}
        for catalog_id, row_id, charge in rows.all():
            flags.setdefault(_independent_plan_id(catalog_id, row_id), set()).add(
                charge is not False
            )
        return flags
    rows = await session.execute(
        select(TenantInsurancePlan.id, TenantInsurancePlan.charge_deposit).where(
            TenantInsurancePlan.tenant_id == tenant_id
        )
    )
    return {str(plan_id): {charge is not False} for plan_id, charge in rows.all()}


def _deposit_rule(seen: set[bool] | None) -> str:
    if not seen or seen == {True}:
        # No row (a legacy name): the booking carries no plan id and the deposit is charged.
        return DEPOSIT_CHARGED
    return DEPOSIT_NOT_CHARGED if seen == {False} else DEPOSIT_VARIES


async def patient_insurance_overview(session: AsyncSession, tenant_id: UUID) -> dict | None:
    """The patient-facing view of one clinic's convenios, or None for an unknown clinic."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    deposit_on = bool(tenant.pix_deposit_enabled)
    insurance = await load_tenant_insurance(session, tenant_id)
    flags = await _charge_flags(session, tenant_id, tenant.insurance_mode) if deposit_on else {}
    plans: list[dict] = []
    for plan in insurance.plans:
        name = str(plan.get("name") or "").strip()
        if not name:
            continue
        note = plan.get("note")
        plan_id = plan.get("id")
        deposit = (
            _deposit_rule(flags.get(plan_id) if plan_id else None)
            if deposit_on
            else DEPOSIT_NOT_CHARGED
        )
        plans.append(
            {
                "name": name,
                "payment_text": insurance_payment_description(plan.get("mechanism")),
                "clinic_note": note.strip() if isinstance(note, str) and note.strip() else None,
                "deposit": deposit,
            }
        )
    return {
        "deposit": {
            "enabled": deposit_on,
            "percent": tenant.pix_deposit_percent if deposit_on else None,
        },
        "plans": plans,
    }
