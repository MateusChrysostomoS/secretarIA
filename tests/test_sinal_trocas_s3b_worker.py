"""TASK-085 R / S3b - the patient's worker for a change on a consultation with a sinal.

Contract: tasks/TASK-085/sinal-trocas/contrato.md (§1 table, §3.6-§3.8). The router hands the worker
the confirmed edit (`calendar_deferred` = the agenda is left to the worker); the worker asks the
money engine for EVERY money field (service, unit, doctor, convênio) on a PAID sinal, re-quotes an
UNPAID one (D-R6) and answers with what really happened.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    AppointmentStatus,
    BookingHold,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import appointment_edit as ae, flow_router as fr
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import (
    adjustment_suspension,
    deposit_lifecycle,
    registry,
    service_change,
)
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared import (
    appointment_edit_apply as apply_module,
    deposit as deposit_module,
    flow_runner,
)
from secretaria.workers.shared.handover import CALENDAR_UNAVAILABLE_MESSAGE
from secretaria.workers.shared.sender import _reply_sender
from secretaria.workers.shared.service_change_actions import service_change_bubbles
from tests._edit_flow_support import EditCalendar
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import sent, wire
from tests._reminders_v2 import WA_ID, add_reminder, outbound_messages, reload_appointment
from tests.payments_support import FakeProvider
from tests.test_appointment_edit_paid_service import _edit, _world

DIFF_TEXT = "Diferença do sinal"


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


@pytest.fixture
def recharges(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def _recharge(
        *, tenant_id, appointment_id, old_amount_cents, waba_token=None, redis=None
    ):
        calls.append((appointment_id, old_amount_cents))
        return None

    monkeypatch.setattr(deposit_lifecycle, "recharge_after_edit", _recharge)
    return calls


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


async def _confirm(world, edit, recap="Serviço: x"):
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\n{recap}"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] in ("text", "buttons", "list")]


async def _rows(db, model):  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(model))).all())


async def _set_tenant(db, world, **fields):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        for name, value in fields.items():
            setattr(tenant, name, value)
        await session.commit()
        await session.refresh(tenant)
        world.tenant = tenant


async def _set_deposit(db, world, **fields):  # noqa: F811
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(**fields)
        )
        await session.commit()


async def _count(db, model) -> int:  # noqa: F811
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------------------------
# D-R5: a clinic that books without waiting for the sinal - the change stands now + the Pix
# --------------------------------------------------------------------------------------------


async def test_after_clinic_dearer_change_is_made_now_and_the_difference_pix_follows(db, provider):  # noqa: F811
    world = await _world(db)
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Avaliação", 30, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"  # written now (D-R5)
    [adjustment] = await _rows(db, DepositAdjustment)
    assert (adjustment.mode, adjustment.status, adjustment.diff_cents) == (
        MODE_APPLIED,
        STATUS_AWAITING_PAYMENT,
        5000,
    )
    assert len(calendar.detail_updates) == 1  # the deferred agenda, once
    assert await _count(db, ProfessionalEditNotice) == 1
    texts = _texts()
    assert texts[0].startswith(ae.EDIT_APPLIED)  # the change IS done
    [pix] = [text for text in texts if "R$ 50,00" in text]
    assert pix.startswith("Pronto, sua consulta já foi alterada para Avaliação.")
    assert pix.rstrip().endswith("```000201pix```")


# --------------------------------------------------------------------------------------------
# D-R4: a clinic that books only after the sinal - nothing written, both times kept
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("deferred", [True, False])
async def test_before_clinic_dearer_change_is_suspended_and_says_both_times_are_kept(
    db,  # noqa: F811
    provider,
    deferred,
):
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    calendar = EditCalendar()
    edit = _edit(world, "Avaliação", 45, calendar)
    edit["calendar_deferred"] = deferred

    await _confirm(world, edit)

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Consulta"  # nothing written
    [adjustment] = await _rows(db, DepositAdjustment)
    assert (adjustment.mode, adjustment.status) == (MODE_SUSPEND, STATUS_AWAITING_PAYMENT)
    assert len(await _rows(db, BookingHold)) == 1  # the new window is held
    assert await _count(db, ProfessionalEditNotice) == 0
    if deferred:
        assert calendar.detail_updates == []
    else:
        # The router had already moved the event: it goes back exactly on the row.
        [(event_id, start, end, summary, _description)] = calendar.detail_updates
        assert event_id == "evt-old" and summary.startswith("Consulta")
        assert end - start == timedelta(minutes=30)
    texts = _texts()
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)  # never "done"
    [pix] = [text for text in texts if "R$ 50,00" in text]
    assert "ficam reservados para você" in pix
    assert pix.rstrip().endswith("```000201pix```")
    async with db() as session:
        assert await adjustment_suspension.is_suspended(
            session, tenant_id=world.tenant.id, appointment_id=world.appointment.id
        )


async def test_a_blocked_change_drops_the_done_recap(db, provider):  # noqa: F811
    world = await _world(db)

    await _confirm(world, _edit(world, "Sem preço", 30, EditCalendar()))

    texts = _texts()
    assert texts[0] == service_change.blocked_text("price_unavailable")
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)


# --------------------------------------------------------------------------------------------
# D-R3: doctor and convênio with a paid sinal go through the engine (no longer refused)
# --------------------------------------------------------------------------------------------


async def _second_doctor(db, world):  # noqa: F811
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant.id, name="Dra. Bia")
        session.add(doctor)
        await session.commit()
        return doctor.id


async def test_a_paid_doctor_change_stands_now_on_the_new_agenda(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    doctor = await _second_doctor(db, world)
    spy = []
    real = service_change.request_paid_change

    async def _spy(session, **kwargs):
        spy.append(kwargs["pending_edit"]["professional_id"])
        return await real(session, **kwargs)

    monkeypatch.setattr(service_change, "request_paid_change", _spy)
    new_agenda = EditCalendar()
    edit = _edit(world, "Consulta", 30, new_agenda)
    edit.update(professional_id=doctor, doctor_changed=True, calendar_changed=True)

    await _confirm(world, edit)

    assert spy == [doctor]
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == doctor
    # Created on the new doctor's agenda and re-pointed; the old one is deleted after the commit.
    assert len(new_agenda.created) == 1
    assert row.google_event_id == "evt123" and edit["google_event_id"] == "evt123"
    assert any(text.startswith(ae.EDIT_APPLIED) for text in _texts())


async def test_a_paid_convenio_change_asks_the_engine(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    calls = []
    real = service_change.request_paid_change

    async def _spy(session, **kwargs):
        calls.append(kwargs["pending_edit"]["insurance"])
        return await real(session, **kwargs)

    monkeypatch.setattr(service_change, "request_paid_change", _spy)
    edit = _edit(world, "Consulta", 30, EditCalendar()) | {"insurance": "Unimed"}

    await _confirm(world, edit)

    assert calls == ["Unimed"]
    assert (await reload_appointment(db, world.appointment.id)).insurance == "Unimed"


async def test_a_paid_edit_of_the_name_only_never_asks_the_engine(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)

    async def _never(*args, **kwargs):
        raise AssertionError("no money field changed")

    monkeypatch.setattr(service_change, "request_paid_change", _never)
    edit = _edit(world, "Consulta", 30, EditCalendar()) | {
        "attendee_name": "João",
        "calendar_deferred": False,
    }

    await _confirm(world, edit)

    assert (await reload_appointment(db, world.appointment.id)).attendee_name == "João"
    assert await _rows(db, DepositAdjustment) == []


# --------------------------------------------------------------------------------------------
# D-R6: an UNPAID sinal is re-quoted - last fallible step, undone on a refusal
# --------------------------------------------------------------------------------------------


async def _awaiting_world(db):  # noqa: F811
    world = await _world(db)
    await _set_deposit(db, world, status=PixDepositStatus.AWAITING, amount_cents=6000)
    return world


async def test_an_unpaid_sinal_is_requoted_after_the_agenda_and_recharged_after_the_commit(
    db,  # noqa: F811
    monkeypatch,
    recharges,
):
    world = await _awaiting_world(db)
    order: list[str] = []
    calendar = EditCalendar()
    real_update = calendar.update_event_details

    async def _update(*args, **kwargs):
        order.append("agenda")
        return await real_update(*args, **kwargs)

    async def _requote(session, *, tenant, appointment, deposit, now):
        order.append("requote")
        assert appointment.appointment_type == "Avaliação"  # the edit is written first
        return "voided"

    calendar.update_event_details = _update
    monkeypatch.setattr(deposit_lifecycle, "requote_awaiting", _requote)

    await _confirm(world, _edit(world, "Avaliação", 30, calendar))

    assert order == ["agenda", "requote"]
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
    assert recharges == [(world.appointment.id, 6000)]
    assert _texts()[0].startswith(ae.EDIT_APPLIED)


@pytest.mark.parametrize("outcome", ["payment_in_flight", "void_failed", "raises"])
async def test_a_refused_requote_undoes_the_edit_and_the_agenda(
    db,  # noqa: F811
    monkeypatch,
    recharges,
    outcome,
):
    world = await _awaiting_world(db)

    async def _requote(session, **kwargs):
        if outcome == "raises":
            raise RuntimeError("provider account")
        return outcome

    monkeypatch.setattr(deposit_lifecycle, "requote_awaiting", _requote)
    calendar = EditCalendar()
    edit = _edit(world, "Avaliação", 30, calendar) | {"calendar_deferred": False}

    await _confirm(world, edit)

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Consulta"  # undone
    assert await _count(db, ProfessionalEditNotice) == 0
    assert recharges == []
    expected = deposit_lifecycle.requote_blocked_text(
        "void_failed" if outcome == "raises" else outcome
    )
    texts = _texts()
    assert texts[0] == expected
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    # The router's agenda write goes back to the committed row (compensation).
    [(event_id, _s, _e, summary, _d)] = calendar.detail_updates
    assert event_id == "evt-old" and summary.startswith("Consulta")
    async with db() as session:  # the flow state of the turn still committed
        deposit = await session.scalar(select(PixDeposit))
    assert deposit.status == PixDepositStatus.AWAITING


async def test_an_unchanged_requote_charges_nothing_new(db, monkeypatch, recharges):  # noqa: F811
    world = await _awaiting_world(db)

    async def _requote(session, **kwargs):
        return "unchanged"

    monkeypatch.setattr(deposit_lifecycle, "requote_awaiting", _requote)

    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
    assert recharges == []


async def test_no_sinal_yet_a_money_change_asks_for_one_after_the_commit(db, recharges):  # noqa: F811
    """OQ-10: the consultation had no sinal; `recharge_after_edit(None)` decides if one is due."""
    world = await _world(db)
    async with db() as session:
        await session.execute(PixDeposit.__table__.delete())
        await session.commit()

    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()))
    assert recharges == [(world.appointment.id, None)]

    recharges.clear()
    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()) | {"attendee_name": "Ana"})
    assert recharges == []  # nothing about the money changed


async def test_a_clinic_without_sinal_never_charges(db, recharges):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, pix_deposit_enabled=False)
    async with db() as session:
        await session.execute(PixDeposit.__table__.delete())
        await session.commit()

    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
    assert recharges == [] and await _rows(db, DepositAdjustment) == []


# --------------------------------------------------------------------------------------------
# A SUSPENDED consultation: old cards may only cancel it
# --------------------------------------------------------------------------------------------


async def _suspend(db, world, *, status=STATUS_AWAITING_PAYMENT, expires_in=timedelta(hours=2)):  # noqa: F811
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        adjustment = DepositAdjustment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            deposit_id=deposit.id,
            appointment_id=world.appointment.id,
            kind="increase",
            mode=MODE_SUSPEND,
            status=status,
            old_service="Consulta",
            new_service="Avaliação",
            diff_cents=5000,
            provider_charge_id="pay_old",
            expires_at=datetime.now(UTC) + expires_in,
            asked_at=datetime.now(UTC) if status == STATUS_AWAITING_CHOICE else None,
            pending_edit={
                "appointment_type": "Avaliação",
                "start_at": world.start_at.isoformat(),
                "end_at": (world.start_at + timedelta(minutes=30)).isoformat(),
            },
        )
        session.add(adjustment)
        await session.commit()
        return adjustment.id


async def test_a_suspended_consultation_is_never_edited_by_an_old_card(db, provider):  # noqa: F811
    world = await _world(db)
    await _suspend(db, world)
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Retorno", 20, calendar) | {"calendar_deferred": False})

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    texts = _texts()
    assert texts[0] == adjustment_suspension.SUSPENDED_VISIT_TEXT
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    assert len(await _rows(db, DepositAdjustment)) == 1


@pytest.mark.parametrize("action", ["remconfirm", "remedit", "remkeep", "remnew", "remresched"])
async def test_old_reminder_buttons_cannot_touch_a_suspended_consultation(db, provider, action):  # noqa: F811
    world = await _world(db)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))
    await _suspend(db, world)

    await tasks._handle_action_button(_reply(world), action, str(rid))

    assert _texts() == [adjustment_suspension.SUSPENDED_VISIT_TEXT]
    row = await reload_appointment(db, world.appointment.id)
    assert (row.status, row.confirmation_count or 0) == (AppointmentStatus.SCHEDULED, 0)


@pytest.mark.parametrize("action", ["apptconfirm", "apptresched"])
async def test_old_appointment_buttons_cannot_touch_a_suspended_consultation(db, provider, action):  # noqa: F811
    world = await _world(db)
    await _suspend(db, world)

    await tasks._handle_action_button(_reply(world), action, str(world.appointment.id))

    assert _texts() == [adjustment_suspension.SUSPENDED_VISIT_TEXT]
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_cancelling_from_an_old_card_is_cancel_both(db, provider):  # noqa: F811
    world = await _world(db)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))
    adjustment_id = await _suspend(db, world)

    await tasks._handle_action_button(_reply(world), "remgiveupyes", str(rid))

    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, adjustment_id)
    assert adjustment.status not in ("awaiting_payment", "awaiting_choice")  # settled (S1)


# --------------------------------------------------------------------------------------------
# The three buttons (D-R4/D-R7): WhatsApp and the Portal, scoped to the sinal's patient
# --------------------------------------------------------------------------------------------


async def test_keep_answers_once_and_replans_the_old_consultation(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)
    replans = []

    async def _followup(tenant_id, appointment_id):
        replans.append(appointment_id)

    monkeypatch.setattr(service_change, "followup_kept_old", _followup)

    await tasks._handle_action_button(_reply(world), "svcdiffkeep", str(adjustment_id))

    async with db() as session:
        assert (await session.get(DepositAdjustment, adjustment_id)).status == STATUS_KEPT_OLD
    assert _texts() == [service_change.kept_old_notice_text("Consulta")]
    assert replans == [world.appointment.id]


async def test_cancel_both_cancels_the_old_consultation_in_one_message(db, provider):  # noqa: F811
    world = await _world(db)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)

    await tasks._handle_action_button(_reply(world), "svcdiffboth", str(adjustment_id))

    async with db() as session:
        assert (await session.get(DepositAdjustment, adjustment_id)).status == STATUS_CANCELLED
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    [text] = _texts()
    assert text.startswith("Certo! A troca foi desfeita") and "Consulta cancelada." in text


async def test_resend_sends_the_new_pix_with_both_times_reserved(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)

    async def _choose(session, **kwargs):
        return service_change.ChoiceOutcome(
            kind="resent",
            adjustment_id=uuid4(),
            appointment_id=world.appointment.id,
            diff_cents=5000,
            pix_copy_paste="000201new",
            expires_at=datetime.now(UTC) + timedelta(minutes=20),
            old_service="Consulta",
            new_service="Avaliação",
        )

    monkeypatch.setattr(service_change, "choose_after_expiry", _choose)

    await tasks._handle_action_button(_reply(world), "svcdiffresend", str(adjustment_id))

    [text] = _texts()
    assert text.startswith("Certo! Enviamos um novo Pix da diferença (R$ 50,00)")
    assert "ficam reservados para você" in text and "20 minutos" in text
    assert text.rstrip().endswith("```000201new```")


@pytest.mark.parametrize(("kind", "resend"), [("no_window", False), ("resend_failed", True)])
async def test_a_resend_that_cannot_happen_shows_the_buttons_again(
    db,  # noqa: F811
    provider,
    monkeypatch,
    kind,
    resend,
):
    world = await _world(db)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)

    async def _choose(session, **kwargs):
        return service_change.ChoiceOutcome(kind=kind, adjustment_id=adjustment_id)

    monkeypatch.setattr(service_change, "choose_after_expiry", _choose)

    await tasks._handle_action_button(_reply(world), "svcdiffresend", str(adjustment_id))

    [card] = [item for item in sent() if item[0] == "buttons"]
    assert card[3] == service_change.choice_buttons(adjustment_id, resend=resend)


async def test_a_choice_tap_of_another_patient_changes_nothing(db, provider):  # noqa: F811
    world = await _world(db)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)
    async with db() as session:  # another patient of the SAME clinic taps the forwarded id
        stranger = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900000000")
        session.add(stranger)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=stranger.id)
        session.add(conversation)
        await session.commit()
    other = SimpleNamespace(conversation=conversation)

    await tasks._handle_action_button(_reply(other), "svcdiffboth", str(adjustment_id))

    async with db() as session:
        assert (
            await session.get(DepositAdjustment, adjustment_id)
        ).status == STATUS_AWAITING_CHOICE
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert _texts() == ["Não encontrei essa troca de consulta."]


async def test_the_portal_routes_the_choice_buttons_as_actions():
    from secretaria.workers.portal import inbound

    assert set(service_change.CHOICE_ACTIONS) <= set(inbound.CHOICE_ACTIONS)


# --------------------------------------------------------------------------------------------
# The reply of each outcome (pure)
# --------------------------------------------------------------------------------------------


def _recap():
    return [TextBubble(body=f"{ae.EDIT_APPLIED}\n\nx"), fr.MenuBubble(body="m", labels=["A"])]


def test_the_suspended_reply_promises_both_times_and_never_says_done():
    outcome = service_change.ServiceChangeResult(
        kind="suspended",
        diff_cents=5000,
        pix_copy_paste="000201pix",
        expires_at=datetime.now(UTC) + timedelta(minutes=12),
        decision="increase_charge",
    )
    bubbles = service_change_bubbles(
        _recap(),
        outcome,
        new_service="Avaliação",
        tenant=SimpleNamespace(deposit_hold_ttl_minutes=30),
    )
    assert not bubbles[0].body.startswith(ae.EDIT_APPLIED)
    assert "ficam reservados para você" in bubbles[0].body
    assert "12 minutos" in bubbles[0].body  # the real deadline, not the clinic's TTL


def test_the_legacy_awaiting_reply_keeps_todays_words():
    outcome = service_change.ServiceChangeResult(
        kind="awaiting_payment",
        diff_cents=5000,
        pix_copy_paste="000201pix",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        decision="increase_charge",
    )
    [bubble, _menu] = service_change_bubbles(
        _recap(),
        outcome,
        new_service="Avaliação",
        tenant=SimpleNamespace(deposit_hold_ttl_minutes=30),
    )
    assert bubble.body == service_change.difference_pix_text("Avaliação", 5000, "000201pix", 30)


def test_the_applied_awaiting_reply_keeps_the_recap_and_adds_the_pix():
    outcome = service_change.ServiceChangeResult(
        kind="applied_awaiting_payment",
        diff_cents=5000,
        pix_copy_paste="000201pix",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        decision="increase_charge",
        label="Avaliação com Dra. Bia",
    )
    recap, pix, _menu = service_change_bubbles(
        _recap(),
        outcome,
        new_service="Avaliação",
        tenant=SimpleNamespace(deposit_hold_ttl_minutes=30),
    )
    assert recap.body.startswith(ae.EDIT_APPLIED)
    assert pix.body.startswith("Pronto, sua consulta já foi alterada para Avaliação com Dra. Bia.")


# --------------------------------------------------------------------------------------------
# The confirmation card (deposit._with_service_difference)
# --------------------------------------------------------------------------------------------


def _card_result(world, current: dict, original: dict):
    body = "Confira os dados da consulta."
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.MenuBubble(body=body, labels=[fr.LABEL_CONFIRM, "Voltar"])],
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft={
            "appointment_id": str(world.appointment.id),
            "current": current,
            "original": original,
            "stage": {},
        },
    )
    return result


def _draft(world, **changes) -> tuple[dict, dict]:
    start = world.start_at.astimezone(deposit_module._tenant_tz(world.tenant)).replace(tzinfo=None)
    base = {
        "service": "Consulta",
        "professional_id": None,
        "start_at": start.isoformat(timespec="minutes"),
        "end_at": (start + timedelta(minutes=30)).isoformat(timespec="minutes"),
        "insurance": None,
        "attendee_name": None,
    }
    return {**base, **changes}, base


async def _card_line(world, **changes) -> str | None:
    current, original = _draft(world, **changes)
    result = await deposit_module._with_service_difference(
        _card_result(world, current, original), world.tenant
    )
    body = result.bubbles[0].body
    return body.split("\n\n", 1)[1] if "\n\n" in body else None


async def test_the_card_of_an_after_clinic_says_the_change_is_made_at_once(db, provider):  # noqa: F811
    world = await _world(db)
    line = await _card_line(world, service="Avaliação")
    assert line == service_change.confirm_card_line_text("increase_charge", 5000, timing="after")


async def test_the_card_of_a_before_clinic_says_both_times_are_kept(db, provider):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    line = await _card_line(world, service="Avaliação")
    assert line == service_change.confirm_card_line_text("increase_charge", 5000, timing="before")
    assert "novo horário fica reservado" in line


async def test_the_card_never_promises_a_reservation_without_room(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *a, **k: None)
    line = await _card_line(world, service="Avaliação")
    assert line.startswith(f"{DIFF_TEXT}: R$ 50,00.") and "reservado" not in line


async def test_the_card_of_a_convenio_change_on_a_paid_sinal_has_a_line(db, provider):  # noqa: F811
    world = await _world(db)
    line = await _card_line(world, service="Retorno", insurance="Unimed")
    assert line is not None and "R$ 70,00" in line


async def test_the_card_without_a_money_change_is_untouched(db, provider):  # noqa: F811
    world = await _world(db)
    assert await _card_line(world, attendee_name="João") is None


async def test_the_card_of_an_unpaid_sinal_shows_the_requote(db, provider):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, pix_deposit_percent=30)
    await _set_deposit(db, world, status=PixDepositStatus.AWAITING, amount_cents=6000)
    line = await _card_line(world, service="Avaliação")
    assert line == deposit_lifecycle.requote_card_line_text(6000, 15000)
    assert await _card_line(world, attendee_name="João") is None


async def test_the_card_of_an_unpaid_sinal_at_the_same_amount_is_untouched(db, provider):  # noqa: F811
    world = await _world(db)
    await _set_deposit(db, world, status=PixDepositStatus.AWAITING, amount_cents=15000)
    assert await _card_line(world, service="Avaliação") is None


async def test_the_ids_of_the_three_buttons_fit_whatsapp():
    for _id, label in service_change.choice_buttons(uuid4()):
        assert len(label) <= 20


def test_apply_reexports_the_money_predicate():
    row = SimpleNamespace(
        appointment_type="Consulta", professional_id=None, unit_id=None, insurance=None
    )
    assert apply_module.money_changes(row, {"appointment_type": "Consulta"}) == frozenset()
    assert apply_module.money_changes(row, {"insurance": "Unimed"}) == frozenset({"insurance"})


# --------------------------------------------------------------------------------------------
# The LEGACY fallback ("before", no room for a suspension) writes only service/time/unit when
# paid: a change of doctor, convênio or who the visit is for is refused BEFORE any charge stays
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [{"insurance": "Unimed"}, {"attendee_name": "João"}])
async def test_the_legacy_fallback_refuses_what_the_payment_would_not_write(
    db,  # noqa: F811
    provider,
    monkeypatch,
    extra,
):
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *a, **k: None)
    calendar = EditCalendar()
    edit = _edit(world, "Avaliação", 30, calendar) | extra | {"calendar_deferred": False}

    await _confirm(world, edit)

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.insurance, row.attendee_name) == ("Consulta", None, None)
    assert await _rows(db, DepositAdjustment) == []
    assert provider.calls == []  # decided BEFORE the engine: no Pix is ever created
    texts = _texts()
    assert texts[0] == apply_module.LEGACY_NO_ROOM_TEXT
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    assert len(calendar.detail_updates) == 1  # the router's write goes back on the row


async def test_the_legacy_fallback_still_carries_a_service_change(db, provider, monkeypatch):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *a, **k: None)

    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()))

    [adjustment] = await _rows(db, DepositAdjustment)
    assert (adjustment.mode, adjustment.status) == (None, STATUS_AWAITING_PAYMENT)
    assert not any(call[0] == "cancel_charge" for call in provider.calls)


async def test_outside_the_legacy_fallback_a_convenio_change_is_suspended(db, provider):  # noqa: F811
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")

    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()) | {"insurance": "Unimed"})

    [adjustment] = await _rows(db, DepositAdjustment)
    assert (adjustment.mode, adjustment.pending_edit["insurance"]) == (MODE_SUSPEND, "Unimed")


async def test_the_card_says_a_legacy_change_it_cannot_carry_is_not_possible(
    db,  # noqa: F811
    provider,
    monkeypatch,
):
    world = await _world(db)
    await _set_tenant(db, world, deposit_timing="before")
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *a, **k: None)

    line = await _card_line(world, service="Avaliação", insurance="Unimed")

    assert line == apply_module.LEGACY_NO_ROOM_CARD_TEXT
    assert "Pix" not in line and provider.calls == []
    # A service change alone still shows the legacy line (the payment does write it).
    legacy = await _card_line(world, service="Avaliação")
    assert legacy.startswith(f"{DIFF_TEXT}: R$ 50,00.") and "reservado" not in legacy


# --------------------------------------------------------------------------------------------
# A failure AFTER the engine wrote an "after" change and created its Pix (review M1): the whole
# edit rolls back, the Pix nobody saw is voided, the agenda is compensated, the patient is told
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["agenda_same", "agenda_new_doctor", "reschedule_race"])
async def test_a_failure_after_an_applied_awaiting_change_voids_its_pix(
    db,  # noqa: F811
    provider,
    monkeypatch,
    failure,
):
    world = await _world(db)
    compensated: list[dict] = []
    real_compensate = flow_runner.compensate_appointment_edit

    async def _compensate(tenant, patient_id, edit):
        compensated.append(edit)
        await real_compensate(tenant, patient_id, edit)

    monkeypatch.setattr(flow_runner, "compensate_appointment_edit", _compensate)
    calendar = EditCalendar(fail_update=failure != "reschedule_race")
    edit = _edit(world, "Avaliação", 30, calendar)
    if failure == "agenda_new_doctor":
        doctor = await _second_doctor(db, world)
        edit.update(professional_id=doctor, doctor_changed=True, calendar_changed=True)
    if failure == "reschedule_race":
        edit.update(
            time_changed=True,
            start_at=world.start_at + timedelta(hours=2),
            end_at=world.start_at + timedelta(hours=2, minutes=30),
        )

        async def _limit_reached(session, *, tenant, appointment):
            return False, 99

        monkeypatch.setattr(deposit_lifecycle, "register_reschedule", _limit_reached)

    await _confirm(world, edit)

    assert ("cancel_charge", "pay_diff_1") in provider.calls
    assert await _rows(db, DepositAdjustment) == []
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.professional_id, row.google_event_id) == (
        "Consulta",
        None,
        "evt-old",
    )
    assert row.start_at.replace(tzinfo=None) == world.start_at.replace(tzinfo=None)
    assert await _count(db, ProfessionalEditNotice) == 0
    assert compensated == [edit]
    assert _texts() == [CALENDAR_UNAVAILABLE_MESSAGE]


# --------------------------------------------------------------------------------------------
# The Portal: a choice tap is an ACTION only when its id was on a card of THIS conversation
# --------------------------------------------------------------------------------------------


async def _portal_world(db):  # noqa: F811
    world = await _world(db, channel=CHANNEL_BRAIN_MESSAGE)
    adjustment_id = await _suspend(db, world, status=STATUS_AWAITING_CHOICE)
    return world, adjustment_id


async def _portal_card(world, adjustment_id, *, resend=True):
    """The choice card as S1's follow-up records it on the Portal."""
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="",
        channel=CHANNEL_BRAIN_MESSAGE,
    )
    client = _reply_sender(reply, world.tenant, None)
    await client.send_buttons(
        world.patient.external_id,
        service_change.choice_message_text("Consulta", "Avaliação", 5000),
        service_change.choice_buttons(adjustment_id, resend=resend),
    )


async def _portal_tap(world, action, target_id):
    label = {
        service_change.ACTION_CHOICE_KEEP: service_change.LABEL_CHOICE_KEEP,
        service_change.ACTION_CHOICE_RESEND: service_change.LABEL_CHOICE_RESEND,
        service_change.ACTION_CHOICE_BOTH: service_change.LABEL_CHOICE_BOTH,
    }[action]
    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text=label,
        interactive_reply_id=f"{action}|{target_id}",
    )
    await _send_bot_reply(reply)
    return reply


async def _adjustment_status(db, adjustment_id):  # noqa: F811
    async with db() as session:
        return (await session.get(DepositAdjustment, adjustment_id)).status


async def test_a_portal_keep_tap_on_the_card_is_the_action(db, provider):  # noqa: F811
    world, adjustment_id = await _portal_world(db)
    await _portal_card(world, adjustment_id)

    reply = await _portal_tap(world, service_change.ACTION_CHOICE_KEEP, adjustment_id)

    assert reply.action_button == (service_change.ACTION_CHOICE_KEEP, str(adjustment_id))
    assert await _adjustment_status(db, adjustment_id) == STATUS_KEPT_OLD
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == service_change.kept_old_notice_text("Consulta")


async def test_a_portal_both_tap_cancels_the_old_consultation(db, provider):  # noqa: F811
    world, adjustment_id = await _portal_world(db)
    await _portal_card(world, adjustment_id)

    await _portal_tap(world, service_change.ACTION_CHOICE_BOTH, adjustment_id)

    assert await _adjustment_status(db, adjustment_id) == STATUS_CANCELLED
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED


async def test_a_portal_resend_without_window_records_the_buttons_again(
    db,  # noqa: F811
    provider,
    monkeypatch,
):
    world, adjustment_id = await _portal_world(db)
    await _portal_card(world, adjustment_id)
    real_choose = service_change.choose_after_expiry

    async def _no_window(session, **kwargs):
        if kwargs["choice"] == service_change.CHOICE_RESEND:
            return service_change.ChoiceOutcome(kind="no_window", adjustment_id=adjustment_id)
        return await real_choose(session, **kwargs)

    monkeypatch.setattr(service_change, "choose_after_expiry", _no_window)

    await _portal_tap(world, service_change.ACTION_CHOICE_RESEND, adjustment_id)

    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    assert [o["id"] for o in cards[-1].interactive["options"]] == [
        button_id
        for button_id, _label in service_change.choice_buttons(adjustment_id, resend=False)
    ]
    assert await _adjustment_status(db, adjustment_id) == STATUS_AWAITING_CHOICE
    # A tap on the RE-SENT card validates too.
    reply = await _portal_tap(world, service_change.ACTION_CHOICE_KEEP, adjustment_id)
    assert reply.action_button == (service_change.ACTION_CHOICE_KEEP, str(adjustment_id))
    assert await _adjustment_status(db, adjustment_id) == STATUS_KEPT_OLD


@pytest.mark.parametrize(
    "action",
    [
        service_change.ACTION_CHOICE_KEEP,
        service_change.ACTION_CHOICE_RESEND,
        service_change.ACTION_CHOICE_BOTH,
    ],
)
async def test_a_forged_portal_choice_id_does_nothing(db, provider, action):  # noqa: F811
    """No card of this conversation offered that id: it is never an action."""
    world, adjustment_id = await _portal_world(db)

    reply = await _portal_tap(world, action, adjustment_id)

    assert reply is None or reply.action_button is None
    assert await _adjustment_status(db, adjustment_id) == STATUS_AWAITING_CHOICE
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED
    assert provider.calls == []
