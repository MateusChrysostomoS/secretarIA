"""TASK-085 / T4a R3 - the patient changes only the UNIT of a consultation whose sinal is PAID.

Dra. Ana, Consulta R$ 200,00 (sinal of 30 % = R$ 60,00, paid at Sede). Centro has its own price.
Before TASK-085 the money ran only when the SERVICE changed; a unit with another price must now
go through the same engine (`service_change`) - and a unit with the SAME price must change
nothing at all versus today (characterization first).

The router of today (`appointment_edit_flow._apply_confirm`) writes the agenda of a unit-only
change at Confirmar (it defers only a SERVICE change on a paid sinal): both shapes are covered,
`calendar_deferred` False (today) and True (a router that defers it).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    Appointment,
    FlowState,
    PixDeposit,
    PixDepositStatus,
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
from secretaria.models.deposit_adjustment import (
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import appointment_edit as ae, flow_router as fr, tenant_config as cfg
from secretaria.services.payments import accounts, registry, service_change
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.workers import tasks
from secretaria.workers.shared import deposit as deposit_module
from secretaria.workers.shared.service_change_actions import service_change_bubbles
from tests._edit_flow_support import EditCalendar
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, sent, wire
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world
from tests.payments_support import FakeProvider

WEEK = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
TZ = "America/Sao_Paulo"


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _world(db, *, centro=None):  # noqa: F811
    """Paid sinal at Sede; `centro` = (price, duration) of Centro's own terms, or None."""
    world = await seed_world(
        db,
        start_at=datetime.now(UTC) + timedelta(days=3),
        google_event_id="evt-old",
        professional_name="Dra. Ana",
    )
    await consent(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.pix_difference_refund_min_days = 2
        tenant.deposit_hold_ttl_minutes = 30
        tenant.business_hours = WEEK
        consulta = Service(
            id=uuid4(), tenant_id=tenant.id, name="Consulta", normalized_name="consulta"
        )
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede", address="Rua A, 1")
        centro_unit = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua B, 2")
        session.add_all([consulta, sede, centro_unit])
        await session.flush()
        doctor = await session.get(Professional, world.appointment.professional_id)
        doctor.appointment_types = [
            {
                "service_id": str(consulta.id),
                "name": "Consulta",
                "price": "R$ 200,00",
                "duration_min": 30,
                "is_active": True,
            }
        ]
        session.add_all(
            [
                ProfessionalUnit(
                    professional_id=doctor.id, unit_id=sede.id, sort_order=0, business_hours=WEEK
                ),
                ProfessionalUnit(
                    professional_id=doctor.id,
                    unit_id=centro_unit.id,
                    sort_order=1,
                    business_hours=WEEK,
                ),
            ]
        )
        if centro is not None:
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant.id,
                    professional_id=doctor.id,
                    service_id=consulta.id,
                    unit_id=centro_unit.id,
                    price=centro[0],
                    duration_min=centro[1],
                )
            )
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.unit_id = sede.id
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=tenant.id,
                appointment_id=appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=6000,
                percent_applied=30,
                status=PixDepositStatus.PAID,
            )
        )
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
        row.status = ACCOUNT_CONNECTED
        await session.commit()
        world.tenant = tenant
    world.sede, world.centro, world.doctor_id = sede.id, centro_unit.id, doctor.id
    return world


def _unit_edit(world, calendar, *, deferred=False, minutes=30) -> dict:
    """What `_apply_confirm` returns for a change of the unit only (same service, doctor, time)."""
    start = world.start_at
    return {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-old",
        "google_event_id": "evt-old",
        "google_event_link": None,
        "appointment_type": "Consulta",
        "professional_id": world.doctor_id,
        "old_professional_id": world.doctor_id,
        "old_google_calendar_source": None,
        "insurance": None,
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=minutes),
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
        "calendar": calendar,
        "patient_name": "Maria",
        "calendar_deferred": deferred,
        "unit_id": world.centro,
    }


async def _confirm(world, edit):
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\nUnidade: Centro"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        tasks._ReplyContext(
            conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
        ),
        result,
        WA_ID,
        tenant=world.tenant,
        waba_token="t",
    )


async def _adjustments(db) -> list[DepositAdjustment]:  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(DepositAdjustment))).all())


async def _notices(db) -> int:  # noqa: F811
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] in ("text", "buttons", "list")]


def _local(world, value: datetime) -> str:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(TZ)).replace(tzinfo=None).isoformat(timespec="minutes")


async def _card(world, *, unit=None) -> str:
    """The edit's confirmation card for a unit change, after `_with_service_difference`."""
    start = _local(world, world.start_at)
    end = _local(world, world.start_at + timedelta(minutes=30))
    base = {
        "service": "Consulta",
        "professional_id": str(world.doctor_id),
        "start_at": start,
        "end_at": end,
        "insurance": None,
        "attendee_name": None,
    }
    draft = {
        "appointment_id": str(world.appointment.id),
        "current": {**base, "unit_id": str(unit or world.centro)},
        "original": {**base, "unit_id": str(world.sede)},
        "stage": {},
    }
    body = "Confira os dados da consulta."
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.MenuBubble(body=body, labels=[fr.LABEL_CONFIRM, "Voltar"])],
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft=draft,
    )
    return (await deposit_module._with_service_difference(result, world.tenant)).bubbles[0].body


CARD = "Confira os dados da consulta."


# --------------------------------------------------------------------------------------------
# characterization: the same price at the new unit -> exactly today's unit-only edit
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("centro", [None, (None, 45), ("R$ 200,00", None)])
async def test_a_unit_change_at_the_same_price_is_todays_edit(db, provider, centro):  # noqa: F811
    """No exception, a duration-only one, or an own price equal to the base: no money at all -
    the row moves once, ONE doctor notice, the worker leaves the agenda to the router as
    always, the patient gets the recap; the card has no difference line."""
    world = await _world(db, centro=centro)
    assert await _card(world) == CARD
    calendar = EditCalendar()

    await _confirm(world, _unit_edit(world, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.centro)
    assert await _notices(db) == 1
    assert await _adjustments(db) == []
    assert provider.calls == []
    assert calendar.detail_updates == []
    texts = _texts()
    assert any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    assert not any("Diferença" in text or "diferença" in text for text in texts)


# --------------------------------------------------------------------------------------------
# R3: another price at the new unit -> the difference of the sinal, like a service change
# --------------------------------------------------------------------------------------------


async def test_the_card_shows_the_difference_of_a_dearer_unit(db, provider):  # noqa: F811
    world = await _world(db, centro=("R$ 250,00", None))  # R$ 75,00 - R$ 60,00
    body = await _card(world)
    line = service_change.confirm_card_line_text("increase_charge", 1500, unit_only=True)
    assert body == f"{CARD}\n\n{line}"
    assert line == (
        "Diferença do sinal: R$ 15,00. Ao confirmar, enviamos o Pix dessa diferença; "
        "a unidade só muda depois do pagamento."
    )
    assert provider.calls == [] and await _adjustments(db) == []  # a preview only


@pytest.mark.parametrize("deferred", [False, True])
async def test_confirming_a_dearer_unit_sends_the_pix_and_changes_nothing(
    db,  # noqa: F811
    provider,
    deferred,
):
    world = await _world(db, centro=("R$ 250,00", None))
    calendar = EditCalendar()

    await _confirm(world, _unit_edit(world, calendar, deferred=deferred))

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.sede)
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == (STATUS_AWAITING_PAYMENT, 1500)
    assert (adjustment.old_service, adjustment.new_service) == (
        "Consulta na Unidade Sede",
        "Consulta na Unidade Centro",
    )
    assert adjustment.pending_edit["unit_id"] == str(world.centro)
    assert adjustment.pending_edit["appointment_type"] == "Consulta"
    assert await _notices(db) == 0
    if deferred:
        assert calendar.detail_updates == []
    else:
        # Today's router already put the event at Centro: it goes back in line with the row.
        [(event_id, _start, _end, _summary, description)] = calendar.detail_updates
        assert event_id == "evt-old"
        assert "Unidade: Sede" in description and "Centro" not in description
    [head] = [text for text in _texts() if "R$ 15,00" in text]
    assert head.startswith(
        "Para trocar sua consulta para Consulta na Unidade Centro, falta pagar a diferença do "
        "sinal: R$ 15,00 via Pix."
    )
    assert head.rstrip().endswith("```000201pix```")
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in _texts())


async def test_confirming_a_cheaper_unit_moves_now_and_asks_about_the_difference(
    db,  # noqa: F811
    provider,
):
    world = await _world(db, centro=("R$ 150,00", None))  # R$ 45,00: R$ 15,00 above
    calendar = EditCalendar()
    assert "R$ 15,00" in await _card(world)

    await _confirm(world, _unit_edit(world, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.centro)
    assert await _notices(db) == 1  # written ONCE, by the money engine
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == (STATUS_ASKED, 1500)
    # Back in line with the row first, then the change: the event ends at Centro.
    assert len(calendar.detail_updates) == 2
    assert "Unidade: Centro" in calendar.detail_updates[-1][4]
    question = service_change.refund_question_text("Consulta na Unidade Centro", 1500)
    assert question in _texts()
    assert question.startswith("Pronto, sua consulta agora é Consulta na Unidade Centro.")


async def test_a_unit_without_a_readable_price_keeps_the_consultation(db, provider):  # noqa: F811
    world = await _world(db, centro=("a combinar", None))
    calendar = EditCalendar()

    await _confirm(world, _unit_edit(world, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert row.unit_id == world.sede
    assert await _adjustments(db) == [] and await _notices(db) == 0
    assert service_change.blocked_text("price_unavailable") in _texts()


def test_the_reply_names_the_place_of_a_unit_only_change():
    outcome = service_change.ServiceChangeResult(
        kind="awaiting_payment",
        diff_cents=1500,
        pix_copy_paste="000201pix",
        decision="increase_charge",
        label="Consulta na Unidade Centro",
    )
    [bubble] = service_change_bubbles([], outcome, new_service="Consulta", tenant=None)
    assert "trocar sua consulta para Consulta na Unidade Centro," in bubble.body
    plain = service_change.ServiceChangeResult(
        kind="awaiting_payment", diff_cents=1500, pix_copy_paste="x", decision="increase_charge"
    )
    [bubble] = service_change_bubbles([], plain, new_service="Exame", tenant=None)
    assert "trocar sua consulta para Exame," in bubble.body


def test_the_unit_label_never_doubles_the_word_unidade():
    assert service_change.unit_change_label("Consulta", "Centro") == "Consulta na Unidade Centro"
    assert (
        service_change.unit_change_label("Consulta", "Unidade Centro")
        == "Consulta na Unidade Centro"
    )
    assert service_change.unit_change_label("Consulta", "  ") == "Consulta"
    assert service_change.unit_change_label("Consulta", None) == "Consulta"
