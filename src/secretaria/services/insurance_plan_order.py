"""The clinic's ONE convênio order in the per-doctor mode (`independent`) - TASK-090 SPEC §4.6.

The owner decided the order is one for the whole clinic (SPEC §3 item 6), but in `independent`
mode each doctor only sees his own plans. So a doctor reorders a SUBSET of the clinic's order, and
that must not erase the place of the plans he does not see (unlike
`insurance_catalog.set_tenant_plan_order`, which un-places every plan left out of the list).

Keys (`models/insurance_plan_order.py`): `catalog:<catalog id>` for a catalog plan (shared by every
doctor who picked it), `custom:<row id>` for a doctor's own "Outro" (never shared).

Readers - the doctor's matrix columns (`api/hub/insurance_acceptance.py::_professional_scope`) and
the patient's list (`insurance_catalog._load_independent_insurance`) - sort with
`clinic_order_sort_key`: placed plans by position, then the never-placed ones by normalized name
(the same tie-break as `insurance_catalog._plan_sort_key`).

This module must not import `insurance_catalog` (that module imports this one).
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.insurance_plan_order import InsurancePlanOrder
from secretaria.models.tenant import Tenant
from secretaria.services.service_catalog import normalize

CATALOG_PREFIX = "catalog:"
CUSTOM_PREFIX = "custom:"


def plan_key_for_catalog(catalog_id: UUID) -> str:
    return f"{CATALOG_PREFIX}{catalog_id}"


def plan_key_for_custom(row_id: UUID) -> str:
    return f"{CUSTOM_PREFIX}{row_id}"


def plan_key_for_row(catalog_id: UUID | None, row_id: UUID) -> str:
    """The key of one `professional_insurance_plans` row (catalog pick or the doctor's "Outro")."""
    if catalog_id is not None:
        return plan_key_for_catalog(catalog_id)
    return plan_key_for_custom(row_id)


def apply_subset_order(current: Sequence[str], subset_new_order: Sequence[str]) -> list[str]:
    """The clinic order after one doctor reorders the plans he sees.

    `current` is the clinic's placed keys in order. Keys of the subset missing from it are appended
    at the end (in the subset's relative order). The indexes held by subset keys are then filled,
    in ascending order, with `subset_new_order`; every other key keeps its index.

    ["A","B","C","D"] + ["D","B"] -> ["A","D","C","B"].
    """
    subset = list(dict.fromkeys(subset_new_order))
    wanted = set(subset)
    placed = list(dict.fromkeys(current))
    known = set(placed)
    extended = placed + [key for key in subset if key not in known]
    slots = [index for index, key in enumerate(extended) if key in wanted]
    result = list(extended)
    for index, key in zip(slots, subset, strict=True):
        result[index] = key
    return result


async def load_clinic_order(session: AsyncSession, tenant_id: UUID) -> dict[str, int]:
    """`plan_key -> position` of every placed plan of the clinic (empty = never ordered)."""
    rows = await session.execute(
        select(InsurancePlanOrder.plan_key, InsurancePlanOrder.position).where(
            InsurancePlanOrder.tenant_id == tenant_id
        )
    )
    return {key: int(position) for key, position in rows.all()}


def clinic_order_sort_key(order: dict[str, int], plan_key: str, name: str) -> tuple[int, int, str]:
    """Placed plans first by position, then the never-placed by normalized name."""
    position = order.get(plan_key)
    if position is None:
        return (1, 0, normalize(name))
    return (0, position, normalize(name))


async def save_subset_order(
    session: AsyncSession, tenant_id: UUID, subset_new_order: Sequence[str]
) -> None:
    """Apply `apply_subset_order` to the clinic's stored order and rewrite `position = index`.

    The caller validated the keys (they are plans of the doctor whose matrix sent them). Locks the
    tenant row first so two saves of the same clinic serialize (a no-op on SQLite). No commit.
    """
    await session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    rows = list(
        await session.scalars(
            select(InsurancePlanOrder)
            .where(InsurancePlanOrder.tenant_id == tenant_id)
            .order_by(InsurancePlanOrder.position, InsurancePlanOrder.plan_key)
        )
    )
    by_key = {row.plan_key: row for row in rows}
    new_order = apply_subset_order([row.plan_key for row in rows], subset_new_order)
    for index, key in enumerate(new_order):
        row = by_key.get(key)
        if row is None:
            session.add(InsurancePlanOrder(tenant_id=tenant_id, plan_key=key, position=index))
        elif row.position != index:
            row.position = index
    await session.flush()
