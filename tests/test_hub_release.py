# ruff: noqa: F811
"""POST /appointments/{id}/release — guards, Google, status, reminders (TASK-032 R4, spec 4.4)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    FlowState,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.services.flow_router import STEP_EDIT_MENU
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import set_google_refresh_token
from tests._edit_flow_support import draft_for
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import set_conversation, wire
from tests._reminders_v2 import NOW, add_reminder, seed_paid_deposit, seed_world
from tests.test_appointment_edit_apply import _apply, _edit

CALENDAR = "/tenants/me/calendar"


class _FakeCalendar:
    deleted: list[tuple[str, str]] = []
    fail_with: Exception | None = None
    during_delete = None  # optional async callable, to simulate a concurrent request

    def __init__(self, label: str = "tenant") -> None:
        self.label = label

    @classmethod
    def from_tenant_config(cls, config):
        return cls("tenant")

    async def cancel_event(self, event_id: str) -> None:
        if type(self).during_delete is not None:
            await type(self).during_delete()
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).deleted.append((self.label, event_id))


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendar)
    _FakeCalendar.deleted = []
    _FakeCalendar.fail_with = None
    _FakeCalendar.during_delete = None
    app.state.arq_pool = None
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _connect(db, tenant_id) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant_id, "fake-refresh-token")
        await session.commit()


async def _release(client: AsyncClient, appointment_id, **body):
    return await client.post(f"{CALENDAR}/appointments/{appointment_id}/release", json=body)


async def _appointment(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _reminder_statuses(db, appointment_id) -> set[str]:
    async with db() as session:
        rows = await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.appointment_id == appointment_id)
        )
        return {r.status for r in rows}


class _TransitionSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def _setup(db, acting, **kwargs):
    kwargs.setdefault("last_inbound_at", NOW)
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    await _connect(db, world.tenant.id)
    return world


async def test_release_frees_the_slot_cancels_the_row_and_the_pending_reminders(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting)
    await add_reminder(db, world, kind="day", status="pending")
    spy = _TransitionSpy()
    monkeypatch.setattr(hub_calendar, "log_status_transition", spy)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled" and body["id"] == str(world.appointment.id)
    assert _FakeCalendar.deleted == [("tenant", world.appointment.google_event_id)]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
    assert await _reminder_statuses(db, world.appointment.id) == {"cancelled"}
    [transition] = spy.calls
    assert transition["reason"] == "unconfirmed" and transition["source"] == "hub"
    assert transition["new_status"] == AppointmentStatus.CANCELLED


async def test_a_second_release_changes_nothing_and_says_why(client: AsyncClient, db, acting):
    world = await _setup(db, acting)

    first = await _release(client, world.appointment.id)
    second = await _release(client, world.appointment.id)

    assert first.status_code == 200 and second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "not_live" and detail["status"] == "cancelled"
    assert len(_FakeCalendar.deleted) == 1  # Google was asked once


async def test_release_with_an_open_alterar_dados_draft_leaves_nothing_dangling(
    client: AsyncClient, db, acting, monkeypatch
):
    """TASK-032 R6 interplay: the patient has an edit draft open on this appointment.

    The release wins (the clinic decided). The draft is left in place on the
    conversation (acceptable: the R6 apply path refuses a cancelled appointment
    and `state_expiry` clears the draft by time); the apply must NOT edit the
    cancelled row nor resurrect reminders.
    """
    wire(monkeypatch, db)  # the worker modules point at the in-memory DB
    world = await _setup(db, acting, google_event_id="evt-old")
    draft = draft_for(
        {
            "id": str(world.appointment.id),
            "google_event_id": "evt-old",
            "appointment_type": "Consulta",
            "professional_id": None,
            "start_at": world.appointment.start_at,
            "end_at": world.appointment.end_at,
            "insurance": world.appointment.insurance,
            "attendee_name": None,
        }
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=STEP_EDIT_MENU,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft=draft.to_json(),
    )

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert _FakeCalendar.deleted == [("tenant", "evt-old")]
    assert await _reminder_statuses(db, world.appointment.id) == set()

    await _apply(world, _edit(world, insurance="Amil"))  # the R6 apply path

    row = await _appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED and row.insurance != "Amil"
    assert await _reminder_statuses(db, world.appointment.id) == set()


async def test_two_concurrent_releases_have_exactly_one_winner(
    client: AsyncClient, db, acting, monkeypatch
):
    """The other request cancels the row while this one is talking to Google."""
    world = await _setup(db, acting)

    async def _other_request_wins() -> None:
        async with db() as session:
            await session.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(status=AppointmentStatus.CANCELLED)
            )
            await session.commit()

    _FakeCalendar.during_delete = _other_request_wins
    calls: list[str] = []

    async def _no_money(*args, **kwargs):
        calls.append("deposit hook")

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_appointment_cancelled", _no_money)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_live"
    assert calls == []  # the loser neither touches the money nor notifies


async def test_a_google_failure_changes_nothing_and_is_retryable(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting)
    await add_reminder(db, world, kind="day", status="pending")
    calls: list[str] = []

    async def _no_money(*args, **kwargs):
        calls.append("deposit hook")

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_appointment_cancelled", _no_money)
    _FakeCalendar.fail_with = RuntimeError("google is down")

    response = await _release(client, world.appointment.id)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "calendar_unavailable"
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert await _reminder_statuses(db, world.appointment.id) == {"pending"}
    assert calls == []

    _FakeCalendar.fail_with = None  # Google is back: the same click now works
    retry = await _release(client, world.appointment.id)
    assert retry.status_code == 200


async def test_the_clinic_without_google_connected_cannot_release(client: AsyncClient, db, acting):
    world = await seed_world(db)
    acting["tenant_id"] = world.tenant.id  # no _connect

    response = await _release(client, world.appointment.id)

    assert response.status_code == 422
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_another_clinics_staff_gets_a_404_and_nothing_happens(
    client: AsyncClient, db, acting
):
    mine = await seed_world(db)
    theirs = await _setup(
        db, acting, phone_number_id="pnid-2", wa_id="5511900000002"
    )  # the caller is THEIR staff

    response = await _release(client, mine.appointment.id)

    assert response.status_code == 404
    assert (await _appointment(db, mine.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []
    assert theirs.tenant.id != mine.tenant.id


async def test_a_malformed_id_is_a_404(client: AsyncClient, db, acting):
    await _setup(db, acting)

    assert (await _release(client, "not-a-uuid")).status_code == 404
    assert (await _release(client, uuid4())).status_code == 404


async def test_a_block_without_a_patient_is_not_releasable(client: AsyncClient, db, acting):
    world = await _setup(db, acting)
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(patient_id=None)
        )
        await session.commit()

    response = await _release(client, world.appointment.id)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_a_patient_appointment"
    assert _FakeCalendar.deleted == []


@pytest.mark.parametrize("status", [AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW])
async def test_a_closed_appointment_is_not_releasable(client: AsyncClient, db, acting, status):
    world = await _setup(db, acting, status=status)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_live"
    assert _FakeCalendar.deleted == []


async def test_a_patient_who_confirmed_meanwhile_needs_an_explicit_override(
    client: AsyncClient, db, acting
):
    world = await _setup(db, acting, confirmation_count=1, status=AppointmentStatus.CONFIRMED)

    refused = await _release(client, world.appointment.id)
    forced = await _release(client, world.appointment.id, release_confirmed=True)

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "already_confirmed"
    assert refused.json()["detail"]["confirmation_count"] == 1
    assert forced.status_code == 200 and forced.json()["status"] == "cancelled"


async def test_an_appointment_without_a_google_event_is_released_without_calling_google(
    client: AsyncClient, db, acting
):
    world = await _setup(db, acting, google_event_id="")

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200
    assert _FakeCalendar.deleted == []


async def test_a_professional_appointment_is_deleted_on_the_professionals_calendar(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting, professional_name="Dra. Ana")

    async def _resolve(session, tenant, professional, *, tenant_config=None, **_):
        assert professional.tenant_id == tenant.id
        return _FakeCalendar("professional")

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _resolve)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200
    assert _FakeCalendar.deleted == [("professional", world.appointment.google_event_id)]


async def test_an_unresolvable_owner_calendar_changes_nothing(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting, professional_name="Dra. Ana")

    async def _boom(*args, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _boom)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "calendar_unresolved"
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []


async def test_a_staff_release_works_with_the_clinic_switch_off(client: AsyncClient, db, acting):
    world = await _setup(db, acting, v2=False)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["status"] == "cancelled"


async def test_the_response_keeps_the_appointment_read_shape(client: AsyncClient, db, acting):
    world = await _setup(db, acting)

    body = (await _release(client, world.appointment.id)).json()

    for key in (
        "id",
        "tenant_id",
        "patient_id",
        "status",
        "start_at",
        "deposit_status",
        "deposit_outcome",
        "confirmation_count",
    ):
        assert key in body
    assert body["deposit_outcome"] is None and body["confirmation_count"] == 0


# ------------------------------------------------------------------- Pix deposit


async def _deposit_status(db, appointment_id):
    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
        )
        return deposit.status


async def _policy(db, tenant_id, policy: str) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.pix_retention_policy = policy
        await session.commit()


async def _paid_world(db, acting, *, hours_ahead: float, policy: str = "total", **kwargs):
    world = await _setup(
        db, acting, start_at=datetime.now(UTC) + timedelta(hours=hours_ahead), **kwargs
    )
    await _policy(db, world.tenant.id, policy)
    await seed_paid_deposit(db, world)
    return world


@pytest.fixture
def refund_ok(monkeypatch: pytest.MonkeyPatch):
    async def _refund(session, tenant, deposit, *, value_cents, full, now):
        deposit.status = (
            PixDepositStatus.CANCELLED_REFUNDED if full else PixDepositStatus.CANCELLED_RETAINED
        )
        return "refunded" if full else "partial_refund"

    monkeypatch.setattr(deposit_lifecycle, "_refund", _refund)


async def test_a_paid_deposit_inside_the_retention_window_needs_the_acknowledgement(
    client: AsyncClient, db, acting
):
    world = await _paid_world(db, acting, hours_ahead=2)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "retention_ack_required"
    assert detail["deposit_outcome"] == "retained" and detail["amount_cents"] == 10000
    assert "100,00" in detail["message"] and "fica com o sinal" in detail["message"]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []  # Google was not touched
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID


async def test_with_the_acknowledgement_the_clinic_keeps_the_deposit(
    client: AsyncClient, db, acting
):
    world = await _paid_world(db, acting, hours_ahead=2)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200
    assert response.json()["deposit_outcome"] == "retained"
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.CANCELLED_RETAINED
    assert len(_FakeCalendar.deleted) == 1


async def test_a_partial_policy_is_announced_as_such(client: AsyncClient, db, acting):
    world = await _paid_world(db, acting, hours_ahead=2, policy="partial")

    detail = (await _release(client, world.appointment.id)).json()["detail"]

    assert detail["deposit_outcome"] == "partial_refund"
    assert "50,00" in detail["message"]


async def test_even_a_full_refund_asks_first_and_then_refunds(
    client: AsyncClient, db, acting, refund_ok
):
    world = await _paid_world(db, acting, hours_ahead=96)

    refused = await _release(client, world.appointment.id)
    accepted = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert refused.status_code == 409
    assert refused.json()["detail"]["deposit_outcome"] == "refunded"
    assert "por inteiro" in refused.json()["detail"]["message"]
    assert accepted.status_code == 200 and accepted.json()["deposit_outcome"] == "refunded"


async def test_an_unpaid_charge_needs_no_acknowledgement(client: AsyncClient, db, acting):
    world = await _setup(db, acting)
    async with db() as session:
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=None,
                amount_cents=10000,
                percent_applied=30,
                status=PixDepositStatus.AWAITING,
            )
        )
        await session.commit()

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["deposit_outcome"] == "voided"


async def test_the_flag_is_harmless_without_a_deposit(client: AsyncClient, db, acting):
    world = await _setup(db, acting)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200 and response.json()["deposit_outcome"] is None
