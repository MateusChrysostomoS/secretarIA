"""TASK-085 R / S7a - the patient's worker and the clinic's edit after all slices merged.

Contract: tasks/TASK-085/sinal-trocas/contrato.md §3.8 / §3.9. Pinned here:

* item 1 - the `EditContext.suspended` shim is gone: every entry that builds the edit's context
  (the turn in EDIT_BOOKING, the reminder card's "Alterar Dados", a typed request) sets
  `suspended=True` for a suspended visit, and an open edit of a visit that got suspended meanwhile
  answers `SUSPENDED_VISIT_TEXT` end to end (the inbound turn), nothing written;
* item 3 - an edit undone after the engine created the Pix of its difference voids it through
  `service_change.void_unseen_charge`; when the provider refuses, the clinic gets ONE
  `unseen_charge_to_void` item AFTER the rollback (patient's worker and the clinic's edit).
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, FlowState, PaymentAttentionItem, Tenant
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.models.payment_attention_item import REASON_UNSEEN_CHARGE_TO_VOID
from secretaria.services import appointment_edit as ae, flow_router as fr
from secretaria.services.payments import adjustment_suspension, deposit_lifecycle, service_change
from secretaria.services.payments.provider import ProviderError
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.shared import appointment_edit_support as support
from tests._edit_flow_support import EditCalendar
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import set_conversation
from tests._reminders_v2 import WA_ID, reload_appointment
from tests.test_appointment_edit_paid_service import _edit, _world
from tests.test_sinal_trocas_s3b_worker import (  # noqa: F401 - fixtures (autouse included)
    _confirm,
    _suspend,
    _texts,
    _wire,
    provider,
)


async def _items(db):  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


# --------------------------------------------------------------------------------------------
# 1. EditContext.suspended - no shim, every entry
# --------------------------------------------------------------------------------------------


def test_the_suspended_shim_is_gone():
    assert not hasattr(support, "_CONTEXT_HAS_SUSPENDED")
    assert not hasattr(support, "make_edit_context")
    assert "suspended" in {name for name in ae.EditContext.__dataclass_fields__}


@pytest.mark.parametrize("suspended", [True, False])
async def test_the_turn_context_says_whether_the_visit_is_suspended(db, provider, suspended):  # noqa: F811
    world = await _world(db)
    if suspended:
        await _suspend(db, world)
    conversation = SimpleNamespace(flow_managing_appointment_id=world.appointment.id)

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        ctx = await support.build_edit_context(session, tenant, None, conversation, None, None)

    assert ctx.suspended is suspended
    assert ctx.paid_deposit is True


async def test_the_context_of_another_clinics_visit_is_never_read(db, provider):  # noqa: F811
    world = await _world(db)
    await _suspend(db, world)
    conversation = SimpleNamespace(flow_managing_appointment_id=world.appointment.id)
    another_clinic = SimpleNamespace(id=uuid4())

    async with db() as session:
        ctx = await support.build_edit_context(
            session, another_clinic, None, conversation, None, None
        )

    assert (ctx.suspended, ctx.paid_deposit) == (False, False)


async def test_a_typed_request_never_opens_the_edit_of_a_suspended_visit(db, provider):  # noqa: F811
    """The suspended visit is hidden from the patient's list (S5): with it as the only one, the
    typed "Alterar Dados" entry has nothing to open (the AI answers instead)."""
    world = await _world(db)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        opened = await support.typed_edit_entry(session, tenant, world.patient.id)
    assert opened is not None and opened.flow_state == FlowState.EDIT_BOOKING
    await _suspend(db, world)

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        assert await support.typed_edit_entry(session, tenant, world.patient.id) is None


async def test_the_card_entry_of_a_suspended_visit_is_refused(db, provider):  # noqa: F811
    """The reminder card's "Alterar Dados" (CONTINUE_EDIT carried by an old button)."""
    world = await _world(db)
    await _suspend(db, world)

    await tasks._handle_action_button(_reply(world), "remedit", str(await _reminder(db, world)))

    assert _texts() == [adjustment_suspension.SUSPENDED_VISIT_TEXT]


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


async def _reminder(db, world):  # noqa: F811
    from datetime import UTC, datetime

    from tests._reminders_v2 import add_reminder

    return await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))


async def test_an_open_edit_of_a_visit_suspended_meanwhile_is_refused_end_to_end(db, provider):  # noqa: F811
    """The patient was in "Alterar Dados" when a change (the clinic's, say) suspended the visit:
    the next inbound turn builds the context, the router reads `suspended` and answers ONE line;
    the draft is dropped and nothing is written."""
    world = await _world(db)
    async with db() as session:
        row = await session.get(Appointment, world.appointment.id)
        tenant = await session.get(Tenant, world.tenant.id)
        draft = ae.EditDraft.from_appointment(
            {
                "id": str(row.id),
                "appointment_type": row.appointment_type,
                "start_at": row.start_at,
                "end_at": row.end_at,
                "professional_id": None,
                "insurance": row.insurance,
                "attendee_name": row.attendee_name,
            },
            ZoneInfo(tenant.timezone),
        )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_MENU,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft=draft.to_json(),
    )
    await _suspend(db, world)

    await _send_bot_reply(
        tasks._ReplyContext(
            conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="1"
        )
    )

    assert adjustment_suspension.SUSPENDED_VISIT_TEXT in _texts()
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    async with db() as session:
        from secretaria.models import Conversation

        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_state != FlowState.EDIT_BOOKING
    assert conversation.flow_edit_draft is None


# --------------------------------------------------------------------------------------------
# 3. the patient's worker: an undone edit voids its unseen Pix; a refused void = one item
# --------------------------------------------------------------------------------------------


async def _failed_agenda_after_applied(db, monkeypatch):  # noqa: F811
    world = await _world(db)
    calendar = EditCalendar(fail_update=True)
    await _confirm(world, _edit(world, "Avaliação", 30, calendar))
    return world


async def test_the_worker_voids_through_the_public_helper(db, provider, monkeypatch):  # noqa: F811
    calls: list = []
    real = service_change.void_unseen_charge

    async def _spy(session, *, tenant_id, charge):
        calls.append(charge.charge_id)
        return await real(session, tenant_id=tenant_id, charge=charge)

    monkeypatch.setattr(service_change, "void_unseen_charge", _spy)
    monkeypatch.setattr(
        service_change, "_void_charge", _never("the private void is the engine's own")
    )

    await _failed_agenda_after_applied(db, monkeypatch)

    assert calls == ["pay_diff_1"]
    assert ("cancel_charge", "pay_diff_1") in provider.calls
    assert await _items(db) == []  # voided: nothing for the clinic


def _never(why):
    async def _fail(*args, **kwargs):
        raise AssertionError(why)

    return _fail


async def test_a_refused_void_in_the_worker_opens_one_clinic_item(db, provider, monkeypatch):  # noqa: F811
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)

    world = await _failed_agenda_after_applied(db, monkeypatch)

    async with db() as session:
        assert list((await session.scalars(select(DepositAdjustment))).all()) == []
    [item] = await _items(db)
    assert item.reason == REASON_UNSEEN_CHARGE_TO_VOID
    assert (item.tenant_id, item.provider_charge_id, item.amount_cents) == (
        world.tenant.id,
        "pay_diff_1",
        5000,
    )
    assert item.patient_id == world.patient.id and item.deposit_id is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Consulta"  # the edit itself stays undone


async def test_a_refused_void_after_the_reschedule_limit_race_opens_the_item(
    db,  # noqa: F811
    provider,  # noqa: F811
    monkeypatch,
):
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    world = await _world(db)

    async def _limit_reached(session, *, tenant, appointment):
        return False, 99

    monkeypatch.setattr(deposit_lifecycle, "register_reschedule", _limit_reached)
    edit = _edit(world, "Avaliação", 30, EditCalendar())
    edit.update(
        time_changed=True,
        start_at=world.start_at + timedelta(hours=2),
        end_at=world.start_at + timedelta(hours=2, minutes=30),
    )

    await _confirm(world, edit)

    assert [item.reason for item in await _items(db)] == [REASON_UNSEEN_CHARGE_TO_VOID]
