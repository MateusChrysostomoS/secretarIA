"""The hub contract the TASK-054 list consumes (TASK-052, spec §5.10, owner 2026-10-12).

Every role may list, mark "devolvido" and accept/refuse "Manter" - but only on what it already sees
in the agenda (`get_agenda_viewer`, the rule of GET /calendar/events): a doctor restricted to his
own never reaches another doctor's offer. Marking and deciding are idempotent and tenant-scoped.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.subscription import AGENDA_SCOPE_CLINIC, AGENDA_SCOPE_OWN
from secretaria.models import Appointment, AppointmentStatus, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_promotion, release_offer as ro, reminder_hooks
from secretaria.services.agenda_visibility import CLINIC_WIDE, AgendaViewer
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import CLOSES_AT_SIX, T, make_released

OFFERS = "/tenants/me/release-offers"
BODY = {"resolved_by_name": "Ana"}


class _FreeCalendar:
    async def check_availability(self, start, end, max_results=50):
        return []

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        return {"id": event_id, "htmlLink": "https://calendar.google.com/x"}

    async def cancel_event(self, event_id):
        return None


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch):
    async def _for_booking(session, tenant, professional_id):
        return _FreeCalendar()

    async def _booked(appointment_id, *, now=None):
        return 0

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    monkeypatch.setattr(reminder_hooks, "after_appointment_booked", _booked)
    yield
    from secretaria.main import app

    for dependency in (get_session, get_current_tenant, get_agenda_viewer):
        app.dependency_overrides.pop(dependency, None)


def _wire(db, tenant_id, viewer: AgendaViewer = CLINIC_WIDE):
    """A logged-in member of `tenant_id` who sees what `viewer` sees (any role)."""
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    app.dependency_overrides[get_agenda_viewer] = lambda: viewer


async def _opened(db, *, now=T, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
        )
        await session.commit()
    return released, offer


async def _do(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        await fn(session, offer=row, now=at, **kw)
        await session.commit()


async def test_the_list_shows_what_the_tab_needs_and_never_another_clinic(client: AsyncClient, db):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)
    _other, foreign = await _opened(db)

    items = (await client.get(OFFERS)).json()["items"]

    assert [i["id"] for i in items] == [str(offer.id)]
    (item,) = items
    assert (item["patient_decision"], item["refund_status"]) == ("cancelled", "due")
    assert item["deposit_status"] == "confirmado_pago" and item["amount_cents"] == 6000
    assert item["can_mark_refunded"] is True and item["can_decide_keep"] is False
    assert item["patient_display_name"] == "Maria"
    assert str(foreign.id) not in str(items)


async def test_unpaid_releases_are_listed_and_pending_only_filters(client: AsyncClient, db):
    released, paid = await _opened(db)
    _wire(db, released.tenant_id)
    async with db() as session:  # an unpaid release of the same clinic, an hour later
        appointment = Appointment(
            id=uuid4(),
            tenant_id=released.tenant_id,
            patient_id=released.patient.id,
            conversation_id=released.conversation.id,
            google_event_id="evt-unpaid",
            appointment_type="Consulta",
            status=AppointmentStatus.CANCELLED,
            start_at=released.appointment.start_at + timedelta(days=1),
            end_at=released.appointment.end_at + timedelta(days=1),
        )
        session.add(appointment)
        await session.flush()
        unpaid = PixDeposit(
            id=uuid4(),
            tenant_id=released.tenant_id,
            appointment_id=appointment.id,
            patient_id=released.patient.id,
            asaas_payment_id="pay_unpaid",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.AWAITING,
        )
        session.add(unpaid)
        tenant = await session.get(Tenant, released.tenant_id)
        await ro.open_offer(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=unpaid,
            now=T + timedelta(hours=1),
        )
        await session.commit()

    everything = (await client.get(OFFERS)).json()["items"]
    pending = (await client.get(OFFERS, params={"pending_only": "true"})).json()["items"]

    assert [i["patient_decision"] for i in everything] == ["not_offered", "no_answer"]
    assert [i["id"] for i in pending] == [str(paid.id)]


async def test_scope_follows_the_agenda(client: AsyncClient, db):
    released, mine = await _opened(db, professional_hours=CLOSES_AT_SIX)
    doctor = released.appointment.professional_id
    async with db() as session:  # another doctor's release in the same clinic
        tenant = await session.get(Tenant, released.tenant_id)
    _wire(
        db, released.tenant_id, viewer=AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=doctor)
    )
    assert [i["id"] for i in (await client.get(OFFERS)).json()["items"]] == [str(mine.id)]

    # A plain doctor never acts on another doctor's offer.
    _wire(
        db, released.tenant_id, viewer=AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=uuid4())
    )
    assert (await client.get(OFFERS)).json()["items"] == []
    await _do(db, ro.choose_cancel, mine.id, T + timedelta(hours=1))
    response = await client.post(f"{OFFERS}/{mine.id}/refunded", json=BODY)
    assert response.status_code == 404

    # A manager who is also a doctor narrows with mine=true; without a professional it is 422.
    _wire(db, tenant.id, viewer=AgendaViewer(scope=AGENDA_SCOPE_CLINIC, professional_id=doctor))
    assert len((await client.get(OFFERS, params={"mine": "true"})).json()["items"]) == 1
    _wire(db, tenant.id, viewer=CLINIC_WIDE)  # a secretary: the whole clinic, no "mine"
    assert (await client.get(OFFERS, params={"mine": "true"})).status_code == 422


async def test_marking_refunded_is_idempotent_and_asks_the_patient_once(client: AsyncClient, db):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)  # any role - the secretary included

    first = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["refund_status"] == "marked" == second.json()["refund_status"]
    async with db() as session:
        checks = list(
            await session.scalars(
                select(DepositReleaseEmail).where(DepositReleaseEmail.kind == "patient_check")
            )
        )
    assert len(checks) == 1


async def test_marking_is_refused_before_the_refund_is_due_or_while_keep_is_pending(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    _wire(db, released.tenant_id)
    response = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    assert (response.status_code, response.json()) == (409, {"detail": {"code": "refund_not_due"}})

    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    response = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    assert response.status_code == 409  # keep_pending, or refund_not_due while nothing is due


async def test_marking_a_reused_deposit_is_409_and_the_list_says_do_not_refund(
    client: AsyncClient, db
):
    from tests.release_offer_support import RELEASED_START, new_appointment

    released, offer = await _opened(db)
    await _do(db, ro.choose_reschedule, offer.id, T + timedelta(hours=1),
              conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        await ro.record_carry_check(
            session, tenant_id=released.tenant_id, offer_id=offer.id, outcome="reusable",
            now=T + timedelta(hours=2),
        )  # fmt: skip
        fresh = await new_appointment(session, released, start=RELEASED_START + timedelta(days=4))
        assert await ro.claim_carry_over(
            session, tenant_id=released.tenant_id, conversation_id=released.conversation.id,
            new_appointment=fresh, now=T + timedelta(hours=2),
        )  # fmt: skip
        await session.commit()
    _wire(db, released.tenant_id)

    response = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)

    assert (response.status_code, response.json()) == (409, {"detail": {"code": "deposit_reused"}})
    (item,) = (await client.get(OFFERS)).json()["items"]
    assert (item["deposit_reused"], item["can_mark_refunded"]) == (True, False)
    assert (item["carry_check"], item["state_note"]) == ("reusable", ro.DEPOSIT_REUSED_LABEL)


async def test_accepting_keep_recreates_the_consultation_and_twice_is_the_same(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)

    first = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["decision"] == "kept" and first.json()["new_appointment_id"]
    assert first.json()["new_appointment_id"] == second.json()["new_appointment_id"]


async def test_accepting_without_a_request_or_refusing_twice(client: AsyncClient, db):
    released, offer = await _opened(db)
    _wire(db, released.tenant_id)
    response = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)
    assert (response.status_code, response.json()) == (409, {"detail": {"code": "not_requested"}})

    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    first = await client.post(f"{OFFERS}/{offer.id}/keep/refuse", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/keep/refuse", json=BODY)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["decision"] == "keep_declined"
    assert first.json()["refund_status"] == "due" and first.json()["can_mark_refunded"] is True


async def test_another_clinics_offer_is_a_404_and_a_bad_body_is_never_echoed(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    other, _ = await _opened(db)
    _wire(db, other.tenant_id)
    for path in ("refunded", "keep/accept", "keep/refuse"):
        assert (await client.post(f"{OFFERS}/{offer.id}/{path}", json=BODY)).status_code == 404
    assert (await client.post(f"{OFFERS}/not-a-uuid/refunded", json=BODY)).status_code == 404

    _wire(db, released.tenant_id)
    response = await client.post(
        f"{OFFERS}/{offer.id}/refunded",
        json={"resolved_by_name": "Ana", "note": "Devolvido ao CPF 123.456.789-09 hoje"},
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": {"code": "note_has_personal_data"}},
    )
