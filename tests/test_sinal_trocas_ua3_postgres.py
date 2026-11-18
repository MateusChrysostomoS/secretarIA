"""TASK-085 R / UA3 - "Outro horário" committing while the silence sweep brings the old visit back,
on Postgres (what SQLite cannot show: row locks, the SAVEPOINT of `flow_runner`'s rebook write).

A change in `awaiting_choice` is both REBOOKABLE (the patient tapped "Outro horário" and is
confirming a new time) and RESTORABLE (its deadline passed: the sweep restores the old slot). On the
secretarIA's own agenda (exclusive writes, real `calendar_events` rows), whatever the order, exactly
ONE of them wins:

* the rebook won -> `rebooked`, the row on the NEW window pointing at the live `difmove` event,
  no live `difkeep`;
* the restore won -> `kept_old`, the row on the OLD window pointing at the live `difkeep` event,
  the `difmove` event discarded by `flow_runner` (`_discard_rebook_event`).

Opt-in like tests/test_service_change_postgres.py: `PAYMENTS_TEST_PG_URL` (a throwaway server;
each test creates and drops its own database).
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.ai.formatter import TextBubble
from secretaria.models import Appointment, Conversation, FlowState
from secretaria.models.calendar_event import CalendarEvent
from secretaria.models.deposit_adjustment import (
    OLD_SLOT_RELEASED,
    STATUS_AWAITING_CHOICE,
    STATUS_KEPT_OLD,
    STATUS_REBOOKED,
    DepositAdjustment,
)
from secretaria.services import booking_hold, flow_router as fr
from secretaria.services.local_calendar import create_event_exclusive
from secretaria.services.payments import old_slot
from secretaria.workers import tasks
from secretaria.workers.shared import flow_runner
from tests import test_service_change_postgres as pg
from tests._reminders_r3 import wire
from tests._reminders_v2 import WA_ID
from tests.test_sinal_trocas_ua1_postgres import NOW, _world

pytestmark = pg.pytestmark
ROUNDS = 8
maker = pg.maker
provider = pg.provider
no_default_hooks = pg.no_default_hooks


async def _live(maker, tenant_id) -> dict[str, CalendarEvent]:
    async with maker() as session:
        rows = await session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.tenant_id == tenant_id, CalendarEvent.status == "confirmed"
            )
        )
        return {row.id: row for row in rows}


async def _round(maker, provider, monkeypatch, order: str) -> str:
    seeded, adjustment_id, local, old_id, (old_start, _old_end), _new = await _world(
        maker, provider, monkeypatch
    )
    tenant, patient, appointment = seeded[0], seeded[1], seeded[2]
    assert await old_slot.vacate_old_slot(tenant.id, adjustment_id) == "released"
    conversation_id = uuid4()
    async with maker() as session:
        session.add(
            Conversation(
                id=conversation_id,
                tenant_id=tenant.id,
                patient_id=patient.id,
                flow_state=FlowState.MANAGE_BOOKING,
                flow_step=fr.STEP_MANAGE_CONFIRM,
            )
        )
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == adjustment_id)
            .values(status=STATUS_AWAITING_CHOICE, expires_at=NOW + timedelta(hours=1))
        )
        await session.commit()

    # The router's half (`_manage_rebook`): the event of the new slot, outside any transaction.
    new_start = old_start + timedelta(days=2)
    new_end = new_start + timedelta(minutes=30)
    move_id = old_slot.rebook_event_id(adjustment_id, calendar=local, source="patient")
    event = await create_event_exclusive(
        local, start=new_start, end=new_end, summary="Consulta - Maria", event_id=move_id
    )
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi remarcada. ✅")],
        flow_state=FlowState.IDLE,
        appointment_reschedule={
            "google_event_id": old_id,
            "start_at": new_start,
            "end_at": new_end,
            "rebook": {
                "adjustment_id": adjustment_id,
                "event_id": event["id"],
                "event_link": event.get("htmlLink"),
                "calendar": local,
            },
        },
    )
    reply = tasks._ReplyContext(conversation_id=conversation_id, patient_ref=WA_ID, inbound_body="")

    async def rebook():
        await flow_runner._apply_flow_result(reply, result, WA_ID, tenant=tenant, waba_token="t")

    async def restore():
        async with maker() as session:
            return await old_slot.restore_old_slot(
                session,
                tenant_id=tenant.id,
                adjustment_id=adjustment_id,
                now=NOW + timedelta(hours=2),
                trigger="silence",
            )

    if order == "rebook_first":
        await rebook()
        await restore()
    elif order == "restore_first":
        await restore()
        await rebook()
    else:
        await asyncio.gather(rebook(), restore())

    async with maker() as session:
        adjustment = await session.get(DepositAdjustment, adjustment_id)
        row = await session.get(Appointment, appointment.id)
    live = await _live(maker, tenant.id)
    keep_id = old_slot.restore_event_id(adjustment_id, calendar=local)
    mine = [event_id for event_id in (move_id, keep_id) if event_id in live]
    assert len(mine) == 1, (order, adjustment.status, list(live))  # never two, never none
    assert row.google_event_id == mine[0]
    if adjustment.status == STATUS_REBOOKED:
        assert mine == [move_id]
        assert row.start_at == new_start
    else:
        assert adjustment.status == STATUS_KEPT_OLD
        assert mine == [keep_id]
        assert row.start_at == old_start
    assert adjustment.old_slot != OLD_SLOT_RELEASED
    return adjustment.status


# One world per database (the seed's charge ids are fixed): sequential both ways, then races.
@pytest.mark.parametrize(
    "order", ["rebook_first", "restore_first", *[f"concurrent-{i}" for i in range(ROUNDS)]]
)
async def test_the_rebook_and_the_silence_restore_decide_once(maker, provider, monkeypatch, order):
    monkeypatch.setattr(booking_hold, "_now", lambda: NOW)
    monkeypatch.setattr(old_slot, "_now", lambda: NOW)
    wire(monkeypatch, maker)
    outcome = await _round(maker, provider, monkeypatch, order.split("-")[0])
    if order == "rebook_first":
        assert outcome == STATUS_REBOOKED
    elif order == "restore_first":
        assert outcome == STATUS_KEPT_OLD
