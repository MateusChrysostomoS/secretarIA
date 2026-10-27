"""The released-consultations list quotes what the clinic HOLDS (TASK-067 session 05).

Owner ruling for the list (session 05): the refundable amount of a released sinal is what the
clinic holds for it - `service_change.held_deposit_cents` (sinal + every PAID service difference -
every difference the patient already asked back, which has its own item) - the same number the
release texts and the refund item already use (`adjustment_queries.held_total_cents`). The list
keeps `amount_cents` as the original sinal and adds `held_cents`, computed for the whole page in
ONE tenant-scoped query (`adjustment_queries.load_adjustment_totals`).
"""

import pytest
from httpx import AsyncClient

from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
from secretaria.core.database import get_session
from secretaria.models.deposit_adjustment import STATUS_REFUND_REQUESTED
from tests import test_service_change as base, test_service_change_cancel as cancel
from tests.test_release_offers_hub import _wire

db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks

OFFERS = "/tenants/me/release-offers"


@pytest.fixture(autouse=True)
def _drop_overrides():
    yield
    from secretaria.main import app

    for dependency in (get_session, get_current_tenant, get_agenda_viewer):
        app.dependency_overrides.pop(dependency, None)


async def _listed(client: AsyncClient, db, seeded) -> dict:
    _wire(db, seeded[0].id)
    (item,) = (await client.get(OFFERS)).json()["items"]
    return item


async def test_a_plain_sinal_holds_its_own_amount(client: AsyncClient, db, provider):
    seeded = await cancel._seed(db)
    offer, _body = await cancel._release(db, seeded)
    await cancel._choose_cancel(db, offer)

    item = await _listed(client, db, seeded)

    assert (item["amount_cents"], item["held_cents"]) == (6000, 6000)


async def test_a_paid_difference_is_part_of_what_the_clinic_holds(
    client: AsyncClient, db, provider
):
    seeded = await cancel._paid_difference(db, provider)
    offer, _body = await cancel._release(db, seeded)
    await cancel._choose_cancel(db, offer)

    item = await _listed(client, db, seeded)

    assert (item["amount_cents"], item["held_cents"]) == (6000, 9000)
    assert item["can_mark_refunded"] is True


async def test_a_difference_already_asked_back_is_not_held_twice(client: AsyncClient, db, provider):
    seeded, asked = await cancel._decrease_asked(db)
    assert await cancel._answer(db, seeded, asked.adjustment_id, accept=True) == (
        STATUS_REFUND_REQUESTED
    )
    offer, _body = await cancel._release(db, seeded)

    item = await _listed(client, db, seeded)

    assert (item["amount_cents"], item["held_cents"]) == (6000, 3000)
