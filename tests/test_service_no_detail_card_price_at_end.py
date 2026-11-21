"""TASK-088 Task 8 - no service card in the middle; money only on the final confirmation.

The owner (2026-11-20): tapping a service goes straight to the next step ("Deseja agendar esse
servico?" is gone); the price leaves the middle of the flow. The final confirmation shows it:
a convenio -> no service value; Particular -> the service value; a clinic whose sinal (Pix) is ON
and charged for this booking -> the sinal AND the consultation's full value, whatever the
convenio (TASK-085 D-R12: a convenio never exempts the sinal).

Router level (no DB) for the flow and the Particular/convenio lines; the sinal lines are folded in
by the worker seam (`workers/shared/deposit.py::_with_booking_money`) from the SAME functions the
real charge uses (`deposit_lifecycle`, `deposit_gate`) - a few tests run it over an in-memory DB.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState, Tenant  # noqa: E402
from secretaria.models.pix_deposit import DEPOSIT_TIMING_AFTER, DEPOSIT_TIMING_BEFORE  # noqa: E402
from secretaria.services import (  # noqa: E402
    flow_router as fr,
    flow_units as fu,
    tenant_config as cfg,
)
from secretaria.services.attendee import LABEL_ATTENDEE_SELF  # noqa: E402
from secretaria.services.payments import accounts, deposit_gate, deposit_lifecycle  # noqa: E402
from secretaria.services.payments.money import format_brl  # noqa: E402
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.workers.shared import deposit as deposit_module  # noqa: E402
from tests.payments_support import make_sessionmaker  # noqa: E402
from tests.test_booking_order_service_first import (  # noqa: E402
    _doctor,
    _list,
    _tenant,
    _units,
    _Walk,
)

ECO = "Ecocardiograma"
CONSULTA = "Consulta"
CONSULTA_SID = uuid4()
ECO_SID = uuid4()
CARD_QUESTION = "Deseja agendar esse serviço?"


def _priced():
    return [
        {
            "name": CONSULTA,
            "duration_min": 30,
            "is_active": True,
            "sort_order": 0,
            "price": "R$ 200,00",
            "long_description": "Uma conversa longa sobre o seu caso.",
            "service_id": str(CONSULTA_SID),
        },
        {
            "name": ECO,
            "duration_min": 30,
            "is_active": True,
            "sort_order": 1,
            "price": "R$ 300,00",
            "service_id": str(ECO_SID),
        },
        {"name": "Avaliação", "duration_min": 30, "is_active": True, "sort_order": 2},
    ]


def _clinic(*, collect=True, units=0, doctors=1):
    """A priced clinic: `doctors` doctors (all do every service), `units` 0..2 units."""
    centro, sul = _units()
    unit_list = [centro, sul][:units]
    roster = [
        _doctor(name, units=unit_list, services=_priced())
        for name in ("Dra. Ana", "Dr. Bruno")[:doctors]
    ]
    tenant = _tenant(collect=collect, doctors=roster)
    tenant.appointment_types = _priced()
    return tenant, roster, unit_list


def _texts(result) -> list[str]:
    out = []
    for bubble in result.bubbles:
        body = getattr(bubble, "body", None)
        if body:
            out.append(body)
    return out


def _all_text(result) -> str:
    return "\n".join(_texts(result))


def _no_card_no_money(result) -> None:
    text = _all_text(result)
    assert CARD_QUESTION not in text
    assert "R$" not in text, text


async def _to_confirmation(walk, service=CONSULTA, plan="ins|particular", doctor=None):
    """Agendar -> pra-quem -> service (-> doctor) (-> unit) (-> convenio) -> day -> slot."""
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap(f"svc|{service}")
    if walk.last.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL:
        await walk.tap(f"prof|{doctor.id}")
    if walk.last.flow_step == fu.STEP_AWAITING_UNIT:
        await walk.tap(_list(walk.last).rows[0][0])
    if walk.last.flow_step == fr.STEP_AWAITING_INSURANCE:
        await walk.tap(plan)
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    await walk.tap(_list(walk.last).rows[0][0])
    assert walk.last.flow_step == fr.STEP_AWAITING_SLOT
    await walk.tap(_list(walk.last).rows[0][0])
    assert walk.last.flow_step == fr.STEP_AWAITING_CONFIRMATION
    return walk.last


def _card(result) -> ButtonBubble:
    [card] = [b for b in result.bubbles if isinstance(b, ButtonBubble)]
    assert card.confirm_label == fr.LABEL_CONFIRM
    return card


# --------------------------------------------------------------------------
# 1. The card is gone from the path
# --------------------------------------------------------------------------


async def test_tap_service_goes_straight_to_next_step():
    # One doctor, two units: the service tap lands on the unit question, no card between.
    tenant, roster, units = _clinic(units=2)
    walk = _Walk(tenant, roster)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    await walk.tap(f"svc|{CONSULTA}")
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    assert walk.last.flow_selected_type == CONSULTA
    # A single-doctor clinic never stores a doctor (as before: the tenant snapshot IS his).
    assert walk.last.flow_selected_professional_id is None
    _no_card_no_money(walk.last)
    assert not [b for b in walk.last.bubbles if isinstance(b, ButtonBubble)]

    # One doctor, no units, no convenio step: straight to the days.
    tenant, roster, _ = _clinic(collect=False)
    walk = _Walk(tenant, roster)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap(f"svc|{CONSULTA}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    _no_card_no_money(walk.last)

    # Two doctors: the clinic catalog tap goes to the doctors, and the doctor tap to the days.
    tenant, roster, _ = _clinic(collect=False, doctors=2)
    walk = _Walk(tenant, roster)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    await walk.tap(f"svc|{CONSULTA}")
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    await walk.tap(f"prof|{roster[1].id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_professional_id == roster[1].id
    _no_card_no_money(walk.last)


async def test_a_clinic_catalog_service_done_by_one_doctor_skips_the_doctor_and_the_card():
    tenant, roster, _ = _clinic(collect=False, doctors=2)
    # Only Dr. Bruno does the echocardiogram.
    roster[0].appointment_types = [s for s in _priced() if s["name"] != ECO]
    walk = _Walk(tenant, roster)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap(f"svc|{ECO}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_professional_id == roster[1].id
    assert walk.last.flow_selected_type == ECO
    _no_card_no_money(walk.last)


async def test_no_service_confirm_card_anywhere_in_order():
    tenant, roster, units = _clinic(units=2, doctors=2)
    walk = _Walk(tenant, roster)
    seen: list[str] = []
    await walk.say(fr.LABEL_BOOK)
    seen.append(_all_text(walk.last))
    await walk.say(LABEL_ATTENDEE_SELF)
    seen.append(_all_text(walk.last))
    await walk.tap(f"svc|{ECO}")
    seen.append(_all_text(walk.last))
    await walk.tap(f"prof|{roster[0].id}")
    seen.append(_all_text(walk.last))
    await walk.tap(f"unit|{units[1].id}")
    seen.append(_all_text(walk.last))
    await walk.tap("ins|particular")
    seen.append(_all_text(walk.last))
    await walk.tap(_list(walk.last).rows[0][0])
    seen.append(_all_text(walk.last))
    assert fr.STEP_AWAITING_SERVICE_CONFIRM not in walk.steps
    # Nothing before the final card says "Deseja agendar" or shows any money, the service's long
    # description included.
    for text in seen:
        assert CARD_QUESTION not in text
        assert "R$" not in text
        assert "conversa longa" not in text
    assert walk.steps == [
        fr.STEP_AWAITING_ATTENDEE_CHOICE,
        fr.STEP_AWAITING_CATALOG_SERVICE,
        fr.STEP_AWAITING_SERVICE_PROFESSIONAL,
        fu.STEP_AWAITING_UNIT,
        fr.STEP_AWAITING_INSURANCE,
        fr.STEP_AWAITING_DAY,
        fr.STEP_AWAITING_SLOT,
    ]


async def test_service_help_pick_goes_straight_on_too(monkeypatch):
    tenant, roster, _ = _clinic(collect=False)

    async def _pick(**kwargs):
        return SimpleNamespace(
            kind="pick", choice=CONSULTA, message="Acho que é a Consulta.", question=None
        )

    monkeypatch.setattr(fr, "run_service_help", _pick)
    walk = _Walk(tenant, roster)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap("svchelp|0")
    assert walk.last.flow_step == fr.STEP_SERVICE_HELP
    await walk.say("não sei qual escolher")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_type == CONSULTA
    assert walk.last.flow_selected_professional_id is None  # single doctor: none stored
    assert CARD_QUESTION not in _all_text(walk.last)
    assert "R$" not in _all_text(walk.last)


async def test_the_unit_line_says_the_duration_but_never_the_money():
    # The echocardiogram takes longer at Zona Sul and costs more there: the unit pick says the
    # minutes (the day list would otherwise be sliced by a length nobody told the patient), but
    # no money - that waits for the final card.
    centro, sul = _units()
    ana = _doctor(units=[centro, sul], services=_priced())
    ana.service_unit_terms = {ECO_SID: {sul.id: UnitTerms("R$ 450,00", 45)}}
    tenant = _tenant(collect=False, doctors=[ana])
    tenant.appointment_types = _priced()
    walk = _Walk(tenant, [ana])
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap(f"svc|{ECO}")
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    await walk.tap(f"unit|{sul.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    text = _all_text(walk.last)
    assert "R$" not in text
    assert "45" in text and "Zona Sul" in text


async def test_a_parked_chat_on_the_old_card_step_still_continues():
    # A conversation parked on `awaiting_service_confirm` on deploy day answers and goes on.
    tenant, roster, _ = _clinic(collect=False)
    walk = _Walk(
        tenant,
        roster,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type=CONSULTA,
        flow_selected_professional_id=roster[0].id,
        flow_attendee_name="",
    )
    await walk.say(fr.LABEL_BOOK_SERVICE)
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    # Resuming the parked step (the patient was silent, the reminder re-draws it) never draws the
    # card again either.
    parked = SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type=CONSULTA,
        flow_selected_professional_id=roster[0].id,
        flow_selected_insurance=None,
        flow_selected_unit_id=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="",
        flow_draft=None,
        flow_selected_day=None,
        flow_selected_slot=None,
    )
    resumed = await fr.resume_bubbles(parked, tenant, walk.agenda, roster)
    assert CARD_QUESTION not in _all_text(resumed)
    assert resumed.flow_step == fr.STEP_AWAITING_DAY


# --------------------------------------------------------------------------
# 2. Money only on the final confirmation (the router's part: Particular / convenio)
# --------------------------------------------------------------------------


async def test_final_confirmation_shows_price_for_particular():
    tenant, roster, _ = _clinic()
    walk = _Walk(tenant, roster)
    result = await _to_confirmation(walk, plan="ins|particular")
    body = _card(result).body
    assert "Valor: R$ 200,00" in body
    # The line is the last of the recap, after the date.
    assert body.rstrip().endswith("Valor: R$ 200,00")
    assert body.index("/20") < body.index("Valor:")


async def test_final_confirmation_hides_price_for_convenio():
    tenant, roster, _ = _clinic()
    walk = _Walk(tenant, roster)
    result = await _to_confirmation(walk, plan="ins|Unimed")
    body = _card(result).body
    assert "R$" not in body and "Valor" not in body
    assert walk.last.flow_selected_insurance == "Unimed"
    # Nor does the unit line or any bubble of the confirmation turn.
    assert "R$" not in _all_text(result)


async def test_no_convenio_step_clinic_shows_the_price_like_particular():
    tenant, roster, _ = _clinic(collect=False)
    walk = _Walk(tenant, roster)
    result = await _to_confirmation(walk)
    assert "Valor: R$ 200,00" in _card(result).body


async def test_a_service_without_a_price_has_no_value_line():
    tenant, roster, _ = _clinic()
    walk = _Walk(tenant, roster)
    result = await _to_confirmation(walk, service="Avaliação")
    assert "R$" not in _card(result).body and "Valor" not in _card(result).body


async def test_the_price_is_the_one_at_the_chosen_unit():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul], services=_priced())
    ana.service_unit_terms = {CONSULTA_SID: {sul.id: UnitTerms("R$ 260,00", None)}}
    tenant = _tenant(collect=False, doctors=[ana])
    tenant.appointment_types = _priced()
    walk = _Walk(tenant, [ana])
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap(f"svc|{CONSULTA}")
    await walk.tap(f"unit|{sul.id}")
    await walk.tap(_list(walk.last).rows[0][0])
    await walk.tap(_list(walk.last).rows[0][0])
    assert "Valor: R$ 260,00" in _card(walk.last).body


async def test_the_money_line_wins_over_a_long_recap():
    # The card body is capped by WhatsApp; the recap gives way, never the money.
    tenant, roster, _ = _clinic(collect=False)
    roster[0].name = "Dra. " + "Ana Maria " * 80
    walk = _Walk(tenant, roster)
    tenant.initial_flows = {"enabled": True, "ai_draft_v2": True}  # the card names the doctor
    result = await _to_confirmation(walk)
    body = _card(result).body
    assert "Dra. Ana Maria" in body  # the long name made it onto the card (and was cut)
    assert len(body) <= 1024
    assert body.rstrip().endswith("Valor: R$ 200,00")


# --------------------------------------------------------------------------
# 3. The sinal lines (worker seam, over an in-memory DB)
# --------------------------------------------------------------------------


@pytest.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(deposit_module, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _seed_tenant(
    maker, *, enabled=True, timing=DEPOSIT_TIMING_AFTER, percent=30, account=True, price="R$ 200,00"
) -> Tenant:
    async with maker() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica",
            phone_number_id=str(uuid4())[:12],
            pix_deposit_enabled=enabled,
            pix_deposit_percent=percent,
            payment_provider="asaas",
            deposit_timing=timing,
            appointment_types=[
                {"name": CONSULTA, "duration_min": 30, "is_active": True, "price": price},
                {"name": "Avaliação", "duration_min": 30, "is_active": True, "price": ""},
            ],
        )
        session.add(tenant)
        await session.flush()
        if account:
            row = await accounts.get_or_create_account_row(
                session, tenant_id=tenant.id, provider="asaas"
            )
            cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
            row.status = "connected"
            row.last_verified_at = datetime.now(UTC)
        await session.commit()
        return tenant


async def _confirmation_result(*, plan, service=CONSULTA):
    """The router's real confirmation result for a Particular / convenio booking."""
    tenant, roster, _ = _clinic()
    walk = _Walk(tenant, roster)
    result = await _to_confirmation(walk, service=service, plan=plan)
    return result


@pytest.mark.parametrize("timing", [DEPOSIT_TIMING_AFTER, DEPOSIT_TIMING_BEFORE])
async def test_deposit_on_shows_deposit_and_total_for_charged_patient(db, timing):
    tenant = await _seed_tenant(db, timing=timing)
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    body = _card(out).body
    # 30 % of R$ 200,00; the consultation's full value; the router's own "Valor" line is gone.
    assert "Sinal (Pix): R$ 60,00" in body
    assert "Valor da consulta: R$ 200,00" in body
    assert "Valor: R$" not in body
    assert body.index("Sinal") < body.index("Valor da consulta")


@pytest.mark.parametrize("timing", [DEPOSIT_TIMING_AFTER, DEPOSIT_TIMING_BEFORE])
async def test_a_convenio_patient_still_sees_the_sinal_and_the_total(db, timing):
    # D-R12: a convenio never exempts the sinal - the patient is told what he WILL pay.
    tenant = await _seed_tenant(db, timing=timing)
    result = await _confirmation_result(plan="ins|Unimed")
    assert "R$" not in _card(result).body  # the router said nothing about money
    out = await deposit_module._with_booking_money(result, tenant)
    body = _card(out).body
    assert "Sinal (Pix): R$ 60,00" in body
    assert "Valor da consulta: R$ 200,00" in body


async def test_deposit_off_convenio_no_price(db):
    tenant = await _seed_tenant(db, enabled=False)
    result = await _confirmation_result(plan="ins|Unimed")
    before = _card(result).body
    out = await deposit_module._with_booking_money(result, tenant)
    assert _card(out).body == before
    assert "R$" not in before and "Valor" not in before


async def test_deposit_off_particular_keeps_only_the_value_line(db):
    tenant = await _seed_tenant(db, enabled=False)
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    body = _card(out).body
    assert "Valor: R$ 200,00" in body
    assert "Sinal" not in body


async def test_a_sinal_that_is_not_charged_shows_no_sinal(db):
    # Under Asaas's R$ 5,00 floor: no charge, so no promise on the card.
    tenant = await _seed_tenant(db, price="R$ 10,00", percent=30)
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    assert "Sinal" not in _card(out).body
    # No usable credential: the real charge would be skipped / refused, so nothing is promised.
    tenant = await _seed_tenant(db, account=False)
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    assert "Sinal" not in _card(out).body
    assert "Valor: R$ 200,00" in _card(out).body


async def test_the_sinal_comes_from_the_same_functions_the_charge_uses(db):
    tenant = await _seed_tenant(db, timing=DEPOSIT_TIMING_BEFORE, percent=25)
    async with db() as session:
        gate = await deposit_gate.quote_deposit(
            session,
            tenant,
            patient_id=uuid4(),
            appointment_type=CONSULTA,
            professional_id=None,
            insurance="Unimed",
            unit_id=None,
        )
        requote = await deposit_lifecycle.requote_amount_cents(
            session,
            tenant=tenant,
            appointment=SimpleNamespace(appointment_type=CONSULTA, professional_id=None),
        )
    assert gate.amount_cents == requote == 5000
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    assert f"Sinal (Pix): {format_brl(gate.amount_cents)}" in _card(out).body


async def test_a_failed_quote_sends_the_card_with_the_particular_line_only(db, monkeypatch):
    tenant = await _seed_tenant(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(deposit_lifecycle, "booking_sinal_preview", _boom)
    result = await _confirmation_result(plan="ins|particular")
    out = await deposit_module._with_booking_money(result, tenant)
    body = _card(out).body
    assert "Valor: R$ 200,00" in body and "Sinal" not in body
    # A convenio patient's card simply goes out as the router drew it.
    result = await _confirmation_result(plan="ins|Unimed")
    out = await deposit_module._with_booking_money(result, tenant)
    assert "R$" not in _card(out).body


async def test_the_seam_only_touches_a_new_booking_confirmation(db):
    tenant = await _seed_tenant(db)
    result = await _confirmation_result(plan="ins|particular")
    # A manage / edit card, a day picker, an appointment in the result: left alone.
    other = fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Escolha um dia")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
    )
    assert (await deposit_module._with_booking_money(other, tenant)) is other
    assert other.bubbles[0].body == "Escolha um dia"
    managing = _confirmation_result_with_appointment(result)
    before = _card(managing).body
    out = await deposit_module._with_booking_money(managing, tenant)
    assert _card(out).body == before


def _confirmation_result_with_appointment(result):
    result.flow_managing_appointment_id = uuid4()
    return result


async def test_the_card_is_decorated_once_and_a_second_pass_does_not_duplicate(db):
    tenant = await _seed_tenant(db)
    result = await _confirmation_result(plan="ins|particular")
    once = await deposit_module._with_booking_money(result, tenant)
    text = _card(once).body
    twice = await deposit_module._with_booking_money(once, tenant)
    assert _card(twice).body == text


async def test_the_whole_card_still_fits_the_whatsapp_body(db):
    tenant = await _seed_tenant(db)
    result = await _confirmation_result(plan="ins|particular")
    result.bubbles[0].body = ("x" * 1000) + "\n" + result.bubbles[0].body
    out = await deposit_module._with_booking_money(result, tenant)
    body = _card(out).body
    assert len(body) <= 1024
    assert "Sinal (Pix): R$ 60,00" in body and "Valor da consulta: R$ 200,00" in body


# --------------------------------------------------------------------------
# 4. The AI's express card is the same card
# --------------------------------------------------------------------------


async def _express(insurance):
    import datetime as dt  # noqa: PLC0415

    from secretaria.services.booking_draft import BookingDraft  # noqa: PLC0415
    from tests.test_booking_draft_resolver import (  # noqa: PLC0415
        DAY,
        _conv,
        _resolve,
        _sole,
        _tenant as _draft_tenant,
    )

    tenant = _draft_tenant(initial_flows={"ai_draft_v2": True}, appointment_types=_priced())
    res = await _resolve(
        BookingDraft(service=CONSULTA, day=DAY, time=dt.time(10, 0)),
        conv=_conv(flow_selected_insurance=insurance),
        tenant=tenant,
        pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    return res.result


async def test_the_ai_express_card_gets_the_same_money_lines(db):
    # Particular: the router's own value line (no deposit)...
    particular = await _express("Particular")
    assert "Valor: R$ 200,00" in _card(particular).body
    # ... a convenio: none ...
    convenio = await _express("Unimed")
    assert "R$" not in _card(convenio).body
    # ... and with a charged sinal both get the worker's two lines, like the slot-tap card.
    tenant = await _seed_tenant(db)
    out = await deposit_module._with_booking_money(particular, tenant)
    body = _card(out).body
    assert "Sinal (Pix): R$ 60,00" in body and "Valor da consulta: R$ 200,00" in body
    assert "Valor: R$" not in body
    out = await deposit_module._with_booking_money(convenio, tenant)
    body = _card(out).body
    assert "Sinal (Pix): R$ 60,00" in body and "Valor da consulta: R$ 200,00" in body
