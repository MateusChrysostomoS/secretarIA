"""TASK-085 R / UH2 - additive hub deltas asked by the UF review (fix round 1).

* I2 - stale intent: POST /reschedule and /cancel accept an optional `rebook_adjustment_id` (the
  `id` of a `kind="rebook"` row of GET /awaiting-payment). When present, the route answers 409
  `appointment_changed` (the existing message) unless that change belongs to this consultation
  and clinic and is still `old_slot_lost` - checked before anything is touched, then again by the
  locked step the route already has (`finish_rebook` on that same change; the cancel's
  `_recheck_rebook_due_cancel`). Absent = today's behaviour.
* I3 - the cancel card must be honest before the click: a `kind="rebook"` row carries the patient's
  channel and names (the same fields and source as GET /events: `Patient.channel`, `Patient.name`,
  `Appointment.attendee_name`), under the same viewer filter as the row itself.
"""

# ruff: noqa: F811

from datetime import timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select

from secretaria.models import AppointmentStatus, Patient, Professional
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services.agenda_visibility import AgendaViewer
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    r7,
    set_appointment,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.test_hub_edit_appointment import provider  # noqa: F401
from tests.test_sinal_trocas_s4_hub import _suspended_world, _window
from tests.test_sinal_trocas_uh_hub import (  # noqa: F401
    _adjustment,
    _deposit,
    _hub_id,
    _lost_world,
    _reschedule,
    _reschedule_body,
    _set_status,
    agenda,
    replans,
)

CHANGED = {
    "code": "appointment_changed",
    "message": "A consulta foi alterada por outra pessoa. Recarregue e tente de novo.",
}


async def _cancel(client, world, **extra):
    return await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True, **extra}
    )


# --------------------------------------------------------------------------
# I2: POST /reschedule with rebook_adjustment_id
# --------------------------------------------------------------------------


async def test_a_rebook_with_the_rows_change_books_the_visit(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    body = {**_reschedule_body(world), "rebook_adjustment_id": str(adjustment.id)}

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    assert response.json()["google_event_id"] == _hub_id(adjustment, body)
    assert (await _adjustment(db)).status == "rebooked"


async def test_a_stale_rebook_never_moves_the_visit_the_patient_already_rebooked(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """The strip was read while the change was `old_slot_lost`; the patient rebooked meanwhile
    (`rebooked`, the row on the patient's own event). Without the id the hub would take the
    ordinary move path and silently move the patient's new slot; with it: 409, nothing touched."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    moved_to = world.start_at + timedelta(days=3)
    await _set_status(db, "rebooked")
    await set_appointment(
        db,
        world.appointment.id,
        google_event_id=f"difmove{adjustment.id.hex}",
        start_at=moved_to,
        end_at=moved_to + timedelta(minutes=30),
        status=AppointmentStatus.RESCHEDULED,
    )
    agenda.clear()
    body = {**_reschedule_body(world, days=5), "rebook_adjustment_id": str(adjustment.id)}

    response = await _reschedule(client, world, body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == CHANGED
    assert agenda == []
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == f"difmove{adjustment.id.hex}"
    assert row.start_at.replace(tzinfo=None) == moved_to.replace(tzinfo=None)
    assert replans == []


async def test_a_rebook_naming_another_change_is_refused(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    agenda.clear()
    body = {**_reschedule_body(world), "rebook_adjustment_id": str(uuid4())}

    response = await _reschedule(client, world, body)

    assert response.status_code == 409 and response.json()["detail"] == CHANGED
    assert agenda == []
    assert (await _adjustment(db)).status == "old_slot_lost"


async def test_a_rebook_of_a_change_back_in_another_state_says_changed_not_suspended(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    await _set_status(db, "awaiting_choice")
    body = {**_reschedule_body(world), "rebook_adjustment_id": str(adjustment.id)}

    response = await _reschedule(client, world, body)

    assert response.status_code == 409 and response.json()["detail"] == CHANGED


async def test_a_rebook_of_a_cancelled_visit_says_changed(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.CANCELLED)
    body = {**_reschedule_body(world), "rebook_adjustment_id": str(adjustment.id)}

    response = await _reschedule(client, world, body)

    assert response.status_code == 409 and response.json()["detail"] == CHANGED


async def test_another_visits_change_cannot_rebook_this_one(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """The id must be THIS consultation's change: another consultation's `old_slot_lost` change
    of the same clinic is refused."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    async with db() as session:
        row = await session.get(DepositAdjustment, adjustment.id)
        other_id = uuid4()
        row.appointment_id = other_id  # the change now belongs to another visit
        await session.commit()
    body = {**_reschedule_body(world), "rebook_adjustment_id": str(adjustment.id)}

    response = await _reschedule(client, world, body)

    assert response.status_code == 409 and response.json()["detail"] == CHANGED


async def test_a_malformed_rebook_id_is_422(client: AsyncClient, db, acting, provider, agenda):
    world = await _lost_world(db, acting, client)
    body = {**_reschedule_body(world), "rebook_adjustment_id": "not-a-uuid"}

    assert (await _reschedule(client, world, body)).status_code == 422


async def test_without_the_id_the_reschedule_is_todays(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    first = _reschedule_body(world)
    assert (await _reschedule(client, world, first)).status_code == 200
    agenda.clear()

    # An ordinary visit now: a plain reschedule moves it, as before (no id = no rebook intent).
    again = await _reschedule(client, world, _reschedule_body(world, days=3))

    assert again.status_code == 200, again.text
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == _hub_id(adjustment, first)


# --------------------------------------------------------------------------
# I2: POST /cancel with rebook_adjustment_id
# --------------------------------------------------------------------------


async def test_a_cancel_with_the_rows_change_cancels_the_visit(
    client: AsyncClient, db, acting, provider, agenda
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)

    response = await _cancel(client, world, rebook_adjustment_id=str(adjustment.id))

    assert response.status_code == 200, response.text
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED


async def test_a_stale_cancel_never_cancels_the_visit_the_patient_already_rebooked(
    client: AsyncClient, db, acting, provider, agenda
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    await _set_status(db, "rebooked")
    patient_event = f"difmove{adjustment.id.hex}"
    await set_appointment(
        db,
        world.appointment.id,
        google_event_id=patient_event,
        status=AppointmentStatus.RESCHEDULED,
    )
    agenda.clear()

    response = await _cancel(client, world, rebook_adjustment_id=str(adjustment.id))

    assert response.status_code == 409 and response.json()["detail"] == CHANGED
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED and row.google_event_id == patient_event
    assert agenda == []  # the patient's event was never deleted
    assert (await _deposit(db)).status.value == "confirmado_pago"


async def test_a_cancel_naming_another_change_is_refused_before_the_agenda(
    client: AsyncClient, db, acting, provider, agenda
):
    world = await _lost_world(db, acting, client)
    agenda.clear()

    response = await _cancel(client, world, rebook_adjustment_id=str(uuid4()))

    assert response.status_code == 409 and response.json()["detail"] == CHANGED
    assert agenda == []
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status != AppointmentStatus.CANCELLED


async def test_without_the_id_a_cancel_is_todays(client: AsyncClient, db, acting, provider, agenda):
    world = await _lost_world(db, acting, client)

    response = await _cancel(client, world)

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# I3: the rebook row says who the patient is and on which channel
# --------------------------------------------------------------------------


async def _patient(db, world) -> Patient:
    async with db() as session:
        visit = await reload_appointment(db, world.appointment.id)
        return (await session.scalars(select(Patient).where(Patient.id == visit.patient_id))).one()


async def test_a_rebook_row_names_the_patient_and_the_channel_like_the_agenda(
    client: AsyncClient, db, acting, provider
):
    world = await _lost_world(db, acting, client)
    patient = await _patient(db, world)
    await set_appointment(db, world.appointment.id, attendee_name="Joana")

    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert row["kind"] == "rebook"
    assert row["patient_name"] == patient.name
    assert row["attendee_name"] == "Joana"
    assert row["patient_channel"] == patient.channel
    text = str(row)
    assert "000201pix" not in text and "pay_diff" not in text


async def test_a_portal_patient_is_said_to_be_one(client: AsyncClient, db, acting, provider):
    world = await _lost_world(db, acting, client)
    patient = await _patient(db, world)
    async with db() as session:
        found = await session.get(Patient, patient.id)
        found.channel = "brain_message"
        await session.commit()

    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert row["patient_channel"] == "brain_message"
    assert row["attendee_name"] is None


async def test_held_windows_never_carry_the_patient(client: AsyncClient, db, acting, provider):
    world = await _suspended_world(db, acting, client)

    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert row["kind"] == "difference"
    assert (row["patient_name"], row["attendee_name"], row["patient_channel"]) == (None, None, None)


async def test_a_restricted_viewer_never_receives_another_doctors_patient(
    client: AsyncClient, db, acting, provider
):
    world = await _lost_world(db, acting, client)
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant.id, name="Dra. Outra")
        session.add(doctor)
        await session.commit()
    view_as(AgendaViewer("own", doctor.id))

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert body == []
