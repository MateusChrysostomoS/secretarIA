"""TASK-085 R / UB1 - D-R14: the clinic changed a price; the sweep tells the patients.

`price_notice_needed` (read-only) decides whether the price in force moves a consultation's sinal
enough to tell the patient; `sweep_price_changes` walks ONE clinic's future live consultations
with a PAID/AWAITING sinal and, for each one that needs it, e-mails a Portal patient a link into
the conversation (an `appointment_reminders` row of kind `price_change`, born `sent`) or opens the
clinic item `price_change_no_email` for anyone it cannot e-mail. Contract: mapa-u.md §2.1-§2.2,
§2.4, §4.5, §7 items 11-21.

Characterization first: a clinic without sinal, a price that did not move, and the rows the sweep
must never touch behave exactly as today (no e-mail, no row, no item).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_COLLECT_DUE,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    REASON_PRICE_CHANGE_NO_EMAIL,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import PixDeposit, PixDepositStatus
from secretaria.services import reminder_delivery, tenant_config as cfg
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.email import EmailOutcome
from secretaria.services.payments import accounts, price_change
from tests.payments_support import FakeRedis, make_sessionmaker

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CHANGED_AT = NOW - timedelta(minutes=2)
START = datetime(2026, 11, 30, 13, 0, tzinfo=UTC)  # 12 days ahead: far from every limit
LINK = "https://portal.test/clinicas/?convite=x&produto=secretaria&lembrete="


def catalog(consulta: str = "R$ 200,00") -> list[dict]:
    return [{"name": "Consulta", "duration_min": 30, "is_active": True, "price": consulta}]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(price_change, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def mail(monkeypatch) -> dict:
    """Records every e-mail; `outcome` is what the mailer answers; the Portal link is fixed."""
    state: dict = {"sent": [], "outcome": EmailOutcome.SENT}

    async def _send(to, template, variables):
        state["sent"].append((to, template, dict(variables)))
        return state["outcome"]

    monkeypatch.setattr(price_change, "send_transactional_email_result", _send)
    monkeypatch.setattr(
        price_change,
        "portal_conversation_link",
        lambda tenant_id, reminder_id: f"{LINK}{reminder_id}",
    )

    async def _no_brain_api(tenant_id, external_id):
        return PatientEmailResult()  # unavailable: the stored copy is used

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _no_brain_api)
    return state


class Recorder:
    """Captures structlog calls (house pattern, see test_build_identity)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def _record(self, event: str, **kwargs: object) -> None:
        self.records.append((event, kwargs))

    info = warning = error = debug = _record

    def fields(self, event: str) -> dict:
        return next(kwargs for name, kwargs in self.records if name == event)


@pytest.fixture
def logs(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(price_change, "logger", recorder)
    return recorder


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


async def seed_tenant(session, *, enabled=True, price="R$ 200,00", account=True) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinica Sol",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=enabled,
        pix_deposit_percent=30,
        payment_provider="asaas",
        appointment_types=catalog(price),
        timezone="America/Sao_Paulo",
    )
    session.add(tenant)
    await session.flush()
    if account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
    await session.flush()
    return tenant


async def seed_visit(
    session,
    tenant: Tenant,
    *,
    portal=True,
    email: str | None = "paciente@example.test",
    patient: Patient | None = None,
    paid_cents: int | None = 6000,
    awaiting_cents: int | None = None,
    start=START,
    status=AppointmentStatus.SCHEDULED,
    deposit_created_at=CHANGED_AT - timedelta(days=3),
):
    if patient is None:
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            channel="brain_message" if portal else "whatsapp",
            external_id=str(uuid4()) if portal else "5511900000000",
            wa_id=None if portal else "5511900000000",
            email=email,
            name="Paciente Teste",
        )
        session.add(patient)
        await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id=f"evt{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone="5511900000000",
        status=status,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    cents = paid_cents if awaiting_cents is None else awaiting_cents
    deposit = PixDeposit(
        id=uuid4(),
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        provider="asaas",
        provider_charge_id=f"pay_{uuid4().hex[:6]}",
        amount_cents=cents,
        percent_applied=30,
        status=PixDepositStatus.PAID if awaiting_cents is None else PixDepositStatus.AWAITING,
        created_at=deposit_created_at,
    )
    session.add(deposit)
    await session.flush()
    return patient, appointment, deposit


async def reprice(db, tenant_id, price: str) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.appointment_types = catalog(price)
        await session.commit()


async def sweep(tenant_id, redis, *, now=NOW, changed_at=CHANGED_AT) -> int:
    return await price_change.sweep_price_changes(
        tenant_id, changed_at=changed_at, now=now, redis=redis
    )


async def notices(db, appointment_id=None) -> list[AppointmentReminder]:
    async with db() as session:
        query = select(AppointmentReminder).where(
            AppointmentReminder.kind == REMINDER_KIND_PRICE_CHANGE
        )
        if appointment_id is not None:
            query = query.where(AppointmentReminder.appointment_id == appointment_id)
        return list(await session.scalars(query))


async def items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def needed(db, appointment_id, *, now=NOW) -> bool:
    async with db() as session:
        appointment = await session.get(Appointment, appointment_id)
        tenant = await session.get(Tenant, appointment.tenant_id)
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
        )
        return await price_change.price_notice_needed(
            session, tenant=tenant, appointment=appointment, deposit=deposit, now=now
        )


# ------------------------------------------------------------------ characterization


async def test_a_clinic_without_sinal_gets_nothing(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session, enabled=False)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 400,00")

    assert await needed(db, appointment.id) is False
    assert await sweep(tenant.id, redis) == 0
    assert mail["sent"] == [] and await notices(db) == [] and await items(db) == []


async def test_a_price_that_did_not_move_tells_nobody(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)  # 30% of R$ 200 = R$ 60 paid
        await session.commit()

    assert await needed(db, appointment.id) is False
    assert await sweep(tenant.id, redis) == 0
    assert mail["sent"] == [] and await notices(db) == [] and await items(db) == []


# ------------------------------------------------------------------ price_notice_needed


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("R$ 240,00", True),  # sinal 72 vs 60: +R$ 12 -> increase_charge
        ("R$ 230,00", False),  # +R$ 9 -> increase_free (settled at the visit)
        ("R$ 160,00", True),  # -R$ 12, 12 days ahead -> decrease_ask
        ("R$ 170,00", False),  # -R$ 9 -> decrease_keep under R$ 10
        ("", False),  # unreadable price -> blocked
    ],
)
async def test_paid_sinal_needs_a_notice_only_for_the_actions_with_something_to_do(
    db, price, expected
):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, price)
    assert await needed(db, appointment.id) is expected


async def test_paid_drop_too_close_for_a_refund_is_still_told_when_it_is_at_least_r10(db):
    async with db() as session:
        tenant = await seed_tenant(session)
        tenant.pix_difference_refund_min_days = 30  # START is 12 days away: decrease_keep
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 160,00")
    assert await needed(db, appointment.id) is True


async def test_paid_raise_without_a_payment_account_is_not_told(db):
    async with db() as session:
        tenant = await seed_tenant(session, account=False)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    assert await needed(db, appointment.id) is False
    # ...but a drop still is (§7 item 19: without an account, raises are out, drops go on).
    await reprice(db, tenant.id, "R$ 160,00")
    assert await needed(db, appointment.id) is True


async def test_awaiting_sinal_needs_a_notice_when_the_new_amount_differs(db):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant, awaiting_cents=6000)
        await session.commit()
    assert await needed(db, appointment.id) is False  # 30% of R$ 200 = R$ 60: same amount
    await reprice(db, tenant.id, "R$ 201,00")
    assert await needed(db, appointment.id) is True  # any change of an unpaid sinal (D-R6)


# ------------------------------------------------------------------ the e-mail


async def test_a_portal_patient_gets_one_email_with_the_link_to_a_born_sent_row(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 1

    [row] = await notices(db)
    assert row.tenant_id == tenant.id and row.appointment_id == appointment.id
    assert row.patient_id == patient.id
    assert row.status == REMINDER_STATUS_SENT  # never pending: the reminder engine would send it
    assert row.channel == REMINDER_CHANNEL_EMAIL and row.with_prompt is False
    assert row.answered_at is None and row.invalidated_at is None and row.warn_due_at is None
    assert row.appointment_start_at.replace(tzinfo=UTC) == START
    [(to, template, variables)] = mail["sent"]
    assert to == "paciente@example.test"
    assert template == price_change.EMAIL_TEMPLATE == "price_change_patient"
    assert variables["link_line"] == f"{LINK}{row.id}"
    assert variables["clinic_name"] == "Clinica Sol"
    assert variables["service"] == "Consulta"
    assert variables["when"] == "30/11/2026 às 10:00"  # the clinic's local time
    # No amount travels in the e-mail: it is computed when the link is opened.
    assert not any("R$" in str(value) for value in variables.values())


async def test_an_unanswered_notice_is_not_sent_again(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    assert await sweep(tenant.id, redis) == 1
    await reprice(db, tenant.id, "R$ 260,00")  # edited again before the patient opened it

    assert await sweep(tenant.id, redis) == 0
    assert len(mail["sent"]) == 1 and len(await notices(db)) == 1


async def test_an_answered_notice_is_rearmed_and_sent_again(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    await sweep(tenant.id, redis)
    [row] = await notices(db)
    async with db() as session:
        stored = await session.get(AppointmentReminder, row.id)
        stored.answered_at = NOW - timedelta(minutes=1)
        stored.answer = "opened"
        await session.commit()
    later = NOW + timedelta(hours=1)

    assert await sweep(tenant.id, redis, now=later, changed_at=later) == 1

    [again] = await notices(db)
    assert again.id == row.id  # the same consultation-version keeps its one row
    assert again.answered_at is None and again.answer is None
    assert again.sent_at.replace(tzinfo=UTC) == later
    assert [m[2]["link_line"] for m in mail["sent"]] == [f"{LINK}{row.id}"] * 2


async def test_at_most_three_price_emails_a_day_per_address(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, *_ = await seed_visit(session, tenant)
        for day in range(1, 4):
            await seed_visit(session, tenant, patient=patient, start=START + timedelta(days=day))
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 3
    assert len(mail["sent"]) == 3
    assert len(await notices(db)) == 3  # the 4th consultation: no row, the next sweep tries
    key = price_change.mail_cap_key(tenant.id, "paciente@example.test", "20261118")
    assert redis.store[key] == 4
    assert "paciente@example.test" not in "".join(redis.store)  # never the address in a key


async def test_an_email_that_did_not_go_out_creates_no_row(db, mail, redis):
    mail["outcome"] = EmailOutcome.SEND_FAILED
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 0
    assert await notices(db) == [] and await items(db) == []

    mail["outcome"] = EmailOutcome.SENT  # the next sweep tries again
    assert await sweep(tenant.id, redis) == 1
    assert len(await notices(db)) == 1


async def test_without_redis_there_is_no_cap_so_no_email(db, mail):
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, None) == 0
    assert mail["sent"] == [] and await notices(db) == []


async def test_the_brain_api_address_wins_over_the_stored_copy(db, mail, redis, monkeypatch):
    async def _brain(tenant_id, external_id):
        return PatientEmailResult(available=True, email="novo@example.test")

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _brain)
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 1
    assert [m[0] for m in mail["sent"]] == ["novo@example.test"]


# ------------------------------------------------------------------ no e-mail -> clinic item


@pytest.mark.parametrize(
    ("portal", "email"), [(False, None), (False, "wa@example.test"), (True, None)]
)
async def test_whatsapp_or_no_email_opens_one_clinic_item_per_consultation(
    db, mail, redis, portal, email
):
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, appointment, _d = await seed_visit(session, tenant, portal=portal, email=email)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 0
    assert await sweep(tenant.id, redis) == 0  # a second sweep: deduped

    assert mail["sent"] == [] and await notices(db) == []
    [item] = await items(db)
    assert item.tenant_id == tenant.id and item.reason == REASON_PRICE_CHANGE_NO_EMAIL
    assert item.patient_id == patient.id and item.deposit_id is None
    assert item.slot_start.replace(tzinfo=UTC) == START and item.service_label == "Consulta"
    assert str(appointment.id) in item.dedupe_key


# ------------------------------------------------------------------ who is left out


async def test_the_sweep_leaves_out_what_mapa_2_2_excludes(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        # starts within PRICE_NOTICE_MIN_LEAD (3 h): settled on the day
        await seed_visit(session, tenant, start=NOW + timedelta(hours=2, minutes=59))
        # a sinal created after the edit was quoted at the new price
        await seed_visit(session, tenant, deposit_created_at=CHANGED_AT + timedelta(seconds=1))
        # cancelled consultation
        await seed_visit(session, tenant, status=AppointmentStatus.CANCELLED)
        # a past one
        await seed_visit(session, tenant, start=NOW - timedelta(days=1))
        # an open adjustment (a question waiting for the patient)
        _p, asked, asked_dep = await seed_visit(session, tenant)
        # a difference the clinic will collect at the visit (never charged twice)
        _p, collect, collect_dep = await seed_visit(session, tenant)
        # a suspended change (the consultation is out of the patient's sight)
        _p, suspended, suspended_dep = await seed_visit(session, tenant)
        for appointment, deposit, status, mode in (
            (asked, asked_dep, STATUS_ASKED, None),
            (collect, collect_dep, STATUS_COLLECT_DUE, None),
            (suspended, suspended_dep, STATUS_AWAITING_PAYMENT, MODE_SUSPEND),
        ):
            session.add(
                DepositAdjustment(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    deposit_id=deposit.id,
                    appointment_id=appointment.id,
                    kind=KIND_INCREASE,
                    status=status,
                    mode=mode,
                    old_service="Consulta",
                    new_service="Consulta",
                    diff_cents=1200,
                )
            )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 0
    assert mail["sent"] == [] and await notices(db) == [] and await items(db) == []


async def test_a_closed_adjustment_does_not_keep_the_consultation_out(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, deposit = await seed_visit(session, tenant)
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=appointment.id,
                kind=KIND_INCREASE,
                status=STATUS_APPLIED,
                old_service="Consulta",
                new_service="Consulta",
                diff_cents=1200,
            )
        )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    assert await sweep(tenant.id, redis) == 1


async def test_only_the_swept_clinic_is_touched(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        other = await seed_tenant(session)
        _p, mine, _d = await seed_visit(session, tenant)
        _p, theirs, _d = await seed_visit(session, other, email="outra@example.test")
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    await reprice(db, other.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 1

    assert [row.appointment_id for row in await notices(db)] == [mine.id]
    assert [m[0] for m in mail["sent"]] == ["paciente@example.test"]
    assert await notices(db, theirs.id) == []


async def test_an_unknown_clinic_is_a_quiet_zero(db, mail, redis):
    assert await sweep(uuid4(), redis) == 0


async def test_one_consultation_error_does_not_stop_the_batch(db, mail, redis, monkeypatch):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, broken, _d = await seed_visit(session, tenant, email="a@example.test")
        _p, fine, _d = await seed_visit(
            session, tenant, email="b@example.test", start=START + timedelta(days=1)
        )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    real = price_change.price_notice_needed

    async def _flaky(session, *, tenant, appointment, deposit, now):
        if appointment.id == broken.id:
            raise RuntimeError("boom")
        return await real(session, tenant=tenant, appointment=appointment, deposit=deposit, now=now)

    monkeypatch.setattr(price_change, "price_notice_needed", _flaky)

    assert await sweep(tenant.id, redis) == 1
    assert [row.appointment_id for row in await notices(db)] == [fine.id]


async def test_the_sweep_walks_every_page(db, mail, redis, monkeypatch, logs):
    monkeypatch.setattr(price_change, "_PAGE_SIZE", 2)
    async with db() as session:
        tenant = await seed_tenant(session)
        for n in range(5):
            await seed_visit(
                session, tenant, email=f"p{n}@example.test", start=START + timedelta(hours=n)
            )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 5, logs.records
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(AppointmentReminder)) == 5


async def test_logs_carry_ids_and_counts_only(db, mail, redis, logs):
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await seed_visit(session, tenant, portal=False, email=None, start=START + timedelta(days=1))
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    await sweep(tenant.id, redis)

    text = repr(logs.records)
    assert logs.fields("price_change_sweep_done") == {
        "sent": 1,
        "item": 1,
        "tenant_id": str(tenant.id),
    }
    for secret in ("paciente@example.test", "Paciente Teste", "5511900000000", LINK):
        assert secret not in text


def test_the_email_template_is_the_one_of_mapa_u_4_6():
    from secretaria.services.email import _TEMPLATES, _SafeDict

    template = _TEMPLATES[price_change.EMAIL_TEMPLATE]
    variables = _SafeDict(
        clinic_name="Clinica Sol",
        service="Consulta",
        when="30/11/2026 às 10:00",
        link_line="https://portal.test/x",
    )
    assert template.subject.format_map(variables) == "O valor da sua consulta mudou — Clinica Sol"
    assert template.body.format_map(variables) == (
        "Olá!\n\nA Clinica Sol atualizou o valor de Consulta, da sua consulta de 30/11/2026 às "
        "10:00. Isso muda o sinal.\n\nPara ver o que muda e resolver, abra a sua conversa:\n"
        "https://portal.test/x\n\n— Clinica Sol"
    )
