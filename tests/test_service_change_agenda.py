"""The agenda side of a service change on a PAID sinal (services/payments/service_change.py).

Task 6 of plan 2026-10-26 (troca de serviço com diferença do sinal):
* `sync_agenda_event` - the ONE helper that puts an appointment's event in line with its row
  after a service change (summary, description, window). The patient edit flow uses it when
  the change stands at Confirmar; the after-payment hook uses it when the Pix of the
  difference is paid. A window that grew is checked first and written exclusively.
* `update_agenda_after_payment` - registered on `ADJUSTMENT_APPLIED_HOOKS`: the webhook
  already wrote the row (money first); the agenda follows. When the new window does not fit
  (or the agenda cannot be reached) an attention item tells the clinic - never two
  overlapping events, never the payment undone.
* The patient gets ONE message per event: the agenda hook sends nothing.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, PaymentAttentionItem, PixDeposit, Tenant
from secretaria.models.appointment_reminder import AppointmentReminder
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.models.payment_attention_item import (
    ATTENTION_REASONS,
    REASON_SERVICE_DIFF_AGENDA_PENDING,
)
from secretaria.plugins import appointment_change_notifications as change_notifications
from secretaria.plugins.appointment_change_notifications import CHANGE_RESCHEDULED
from secretaria.schemas.webhook import decode_action_id
from secretaria.services.appointment_status import SOURCE_FLOW, SOURCE_HUB
from secretaria.services.calendar import CalendarSlotTakenError, CalendarUnavailableError
from secretaria.services.payments import event_processor, service_change
from secretaria.services.payments.attention_view import REASON_TEXT
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from tests import test_service_change as base, test_service_change_webhook as hook_base

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


class AgendaCalendar:
    """The two calls the sync makes, recorded."""

    def __init__(self, *, free: bool = True, down: bool = False):
        self.free = free
        self.down = down
        self.checked: list[tuple] = []
        self.updates: list[tuple] = []

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        if self.down:
            raise CalendarUnavailableError("down")
        self.checked.append((start, end, ignore_event_id))
        return self.free

    async def update_event_details(self, event_id, start, end, summary, description=""):
        if self.down:
            raise CalendarUnavailableError("down")
        self.updates.append((event_id, start, end, summary, description))
        return {"id": event_id}


async def _row(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


# --------------------------------------------------------------------------
# sync_agenda_event
# --------------------------------------------------------------------------


async def test_sync_writes_service_and_window_of_the_row(db):
    async with db() as session:
        tenant, _patient, appointment, _deposit = await base._seed(session)
        appointment = await session.get(Appointment, appointment.id)
        appointment.appointment_type = "Avaliação"
        appointment.end_at = appointment.start_at + timedelta(minutes=60)
        calendar = AgendaCalendar()

        await service_change.sync_agenda_event(
            session, tenant, appointment, calendar, patient_name="Maria", window_changed=True
        )

    [(start, end, ignore)] = calendar.checked
    assert ignore == "evt-1" and (end - start) == timedelta(minutes=60)
    [(event_id, _start, end, summary, description)] = calendar.updates
    assert event_id == "evt-1"
    assert summary == "Avaliação - Maria"
    assert "Avaliação" in description


async def test_sync_without_a_window_change_does_not_check_the_slot(db):
    async with db() as session:
        tenant, _patient, appointment, _deposit = await base._seed(session)
        appointment = await session.get(Appointment, appointment.id)
        calendar = AgendaCalendar(free=False)

        await service_change.sync_agenda_event(
            session, tenant, appointment, calendar, patient_name=None, window_changed=False
        )

    assert calendar.checked == [] and len(calendar.updates) == 1
    assert calendar.updates[0][3] == "Consulta - Paciente"


async def test_sync_refuses_a_window_that_is_not_free(db):
    async with db() as session:
        tenant, _patient, appointment, _deposit = await base._seed(session)
        appointment = await session.get(Appointment, appointment.id)
        calendar = AgendaCalendar(free=False)

        with pytest.raises(CalendarSlotTakenError):
            await service_change.sync_agenda_event(
                session, tenant, appointment, calendar, patient_name="Maria", window_changed=True
            )
    assert calendar.updates == []


async def test_sync_refuses_a_window_another_patient_is_paying_for(db, monkeypatch):
    async def held(*args, **kwargs):
        return True

    monkeypatch.setattr(service_change.booking_hold, "live_hold_overlaps", held)
    async with db() as session:
        tenant, _patient, appointment, _deposit = await base._seed(session)
        appointment = await session.get(Appointment, appointment.id)
        calendar = AgendaCalendar()

        with pytest.raises(CalendarSlotTakenError):
            await service_change.sync_agenda_event(
                session, tenant, appointment, calendar, patient_name="Maria", window_changed=True
            )
    assert calendar.updates == []


# --------------------------------------------------------------------------
# update_agenda_after_payment (the applied hook)
# --------------------------------------------------------------------------


async def _applied(db, provider):
    seeded, created = await hook_base._awaiting(db, provider)
    hook_base._paid(provider, created)
    result = await hook_base._process(db, seeded[0])
    assert result.outcome == "adjustment_applied"
    return seeded, created


async def _run_hook(db, seeded, created, calendar, monkeypatch):
    async def resolver(session, tenant, appointment):
        return calendar

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolver)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        notice = await service_change._notice_for(session, tenant, created.adjustment_id)
        await service_change.update_agenda_after_payment(session, tenant, notice)


async def test_the_agenda_hook_is_registered_before_the_patient_notice():
    hooks = service_change._DEFAULT_APPLIED_HOOKS
    assert hooks[0] is service_change.update_agenda_after_payment
    assert service_change.notify_patient_adjustment_applied in hooks


async def test_a_paid_difference_moves_the_agenda_event_to_the_new_service(
    db, provider, monkeypatch
):
    seeded, created = await _applied(db, provider)
    calendar = AgendaCalendar()

    await _run_hook(db, seeded, created, calendar, monkeypatch)

    [(event_id, start, end, summary, _description)] = calendar.updates
    assert event_id == "evt-1" and summary == "Avaliação - Maria"
    assert (end - start) == timedelta(minutes=60)
    assert calendar.checked  # 30 -> 60 minutes: the longer window was checked first
    assert await _items(db) == []


async def test_a_new_window_that_no_longer_fits_opens_an_item_and_keeps_the_payment(
    db, provider, monkeypatch
):
    seeded, created = await _applied(db, provider)
    calendar = AgendaCalendar(free=False)

    await _run_hook(db, seeded, created, calendar, monkeypatch)

    assert calendar.updates == []  # never two overlapping events
    [item] = await _items(db)
    assert item.reason == REASON_SERVICE_DIFF_AGENDA_PENDING
    assert item.deposit_id == seeded[3].id and item.service_label == "Avaliação"
    assert (await _row(db, seeded[2].id)).appointment_type == "Avaliação"  # the money stands
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert adjustment.status == "applied"
    # Run again (a retried job): still one item.
    await _run_hook(db, seeded, created, calendar, monkeypatch)
    assert len(await _items(db)) == 1


async def test_an_unreachable_agenda_opens_the_same_item(db, provider, monkeypatch):
    seeded, created = await _applied(db, provider)

    await _run_hook(db, seeded, created, None, monkeypatch)

    [item] = await _items(db)
    assert item.reason == REASON_SERVICE_DIFF_AGENDA_PENDING


async def test_a_cancelled_consultation_is_left_alone(db, provider, monkeypatch):
    seeded, created = await _applied(db, provider)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        from secretaria.models import AppointmentStatus

        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    calendar = AgendaCalendar()

    await _run_hook(db, seeded, created, calendar, monkeypatch)

    assert calendar.updates == [] and await _items(db) == []


async def test_the_patient_gets_one_message_when_the_difference_is_paid(
    db, provider, monkeypatch
):
    seeded, created = await _applied(db, provider)
    sent: list = []

    async def fake_deliver(session, tenant, patient, outgoing, **kw):
        sent.append(outgoing)

    async def resolver(session, tenant, appointment):
        return AgendaCalendar()

    monkeypatch.setattr(service_change.notify, "deliver", fake_deliver)
    monkeypatch.setattr(service_change, "calendar_for_appointment", resolver)
    monkeypatch.setattr(
        service_change,
        "ADJUSTMENT_APPLIED_HOOKS",
        list(service_change._DEFAULT_APPLIED_HOOKS),
    )
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await service_change.run_applied_hooks(session, tenant, created.adjustment_id)

    [[text]] = sent
    assert "Avaliação" in text.body


class FakeRedis:
    """Records arq enqueues (the A7 "Consulta remarcada" job)."""

    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


async def _moved_awaiting(db, provider, monkeypatch, *, source):  # noqa: F811
    """A dearer service that ALSO moves the time one day later, waiting for its Pix; the
    clinic plans reminders and one is pending for the OLD time."""
    monkeypatch.setattr(core_database, "async_session_factory", db)
    async with db() as session:
        seeded = await base._seed(session)
    tenant, patient, appointment, deposit = seeded
    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        tenant_row.reminders_v2_enabled = True
        row = await session.get(Appointment, appointment.id)
        session.add(
            AppointmentReminder(
                id=uuid4(),
                tenant_id=tenant.id,
                appointment_id=row.id,
                patient_id=patient.id,
                kind="day",
                appointment_start_at=row.start_at,
                due_at=row.start_at - timedelta(days=1),
                status="pending",
                channel="whatsapp",
                with_prompt=True,
                attempts=0,
                warn_due_at=row.start_at - timedelta(hours=22),
            )
        )
        deposit_row = await session.get(PixDeposit, deposit.id)
        edit = base._edit(row, "Avaliação", 60)
        edit["start_at"] = row.start_at + timedelta(days=1)
        edit["end_at"] = edit["start_at"] + timedelta(minutes=60)
        edit["time_changed"] = True
        created = await service_change.request_service_change(
            session, tenant=tenant_row, appointment=row, deposit=deposit_row,
            new_service="Avaliação", pending_edit=edit, now=NOW, source=source,
        )
        await session.commit()
        new_start = edit["start_at"]
    assert created.kind == "awaiting_payment"
    hook_base._paid(provider, created)
    return seeded, created, new_start


async def _deliver(db, tenant_id, result, redis):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        await event_processor.run_post_commit(session, tenant, result, redis=redis)


async def _reminders(db, appointment_id):  # noqa: F811
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(AppointmentReminder).where(
                        AppointmentReminder.appointment_id == appointment_id
                    )
                )
            ).all()
        )


def _naive(value):
    return value.replace(tzinfo=None) if value.tzinfo else value


async def test_a_paid_difference_that_also_moved_the_time_replans_and_tells_once(
    db, provider, monkeypatch  # noqa: F811
):
    """The time moves only when the Pix is paid: the reschedule counts then, the reminders
    are planned for the NEW time and ONE "Consulta remarcada" (A7) is queued - a redelivered
    webhook queues nothing more."""
    seeded, created, new_start = await _moved_awaiting(
        db, provider, monkeypatch, source=SOURCE_FLOW
    )
    tenant, _patient, appointment, deposit = seeded
    redis = FakeRedis()

    first = await hook_base._process(db, tenant)
    await _deliver(db, tenant.id, first, redis)
    again = await hook_base._process(db, tenant, event_id="evt_diff_redelivered")
    await _deliver(db, tenant.id, again, redis)

    assert (first.outcome, again.outcome) == ("adjustment_applied", "adjustment_already_applied")
    async with db() as session:
        deposit_row = await session.get(PixDeposit, deposit.id)
    assert deposit_row.reschedule_count == 1
    rows = await _reminders(db, appointment.id)
    old = [r for r in rows if _naive(r.appointment_start_at) == _naive(appointment.start_at)]
    assert old and all(r.status != "pending" for r in old)
    planned = [r for r in rows if r.status == "pending"]
    assert planned
    assert all(_naive(r.appointment_start_at) == _naive(new_start) for r in planned)
    [(name, args, _kwargs)] = redis.jobs
    assert name == change_notifications.JOB_NAME
    assert args[:3] == (str(tenant.id), str(appointment.id), CHANGE_RESCHEDULED)


async def test_a_clinic_initiated_paid_move_replans_but_sends_no_patient_change_notice(
    db, provider, monkeypatch  # noqa: F811
):
    """A7 is for PATIENT paths only (the hub never announces its own changes)."""
    seeded, _created, new_start = await _moved_awaiting(
        db, provider, monkeypatch, source=SOURCE_HUB
    )
    tenant, _patient, appointment, _deposit = seeded
    redis = FakeRedis()

    result = await hook_base._process(db, tenant)
    await _deliver(db, tenant.id, result, redis)

    planned = [r for r in await _reminders(db, appointment.id) if r.status == "pending"]
    assert planned and all(
        _naive(r.appointment_start_at) == _naive(new_start) for r in planned
    )
    assert redis.jobs == []


async def test_a_paid_difference_without_a_move_queues_no_change_notice(
    db, provider, monkeypatch  # noqa: F811
):
    seeded, created = await _applied(db, provider)
    redis = FakeRedis()
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await service_change.run_applied_hooks(
            session, tenant, created.adjustment_id, redis=redis
        )
    assert redis.jobs == []


# --------------------------------------------------------------------------
# the expiry sweep never starves a healthy row behind stuck ones
# --------------------------------------------------------------------------


async def test_stuck_awaiting_rows_never_starve_a_healthy_expired_one(db, provider, monkeypatch):  # noqa: F811
    """Three differences whose provider check fails (they stay `awaiting_payment`) expire
    before a healthy one. With a batch of 2 the healthy one is still expired within two runs."""
    monkeypatch.setattr(service_change, "_SWEEP_OFFSET", 0)
    async with db() as session:
        tenant, _patient, appointment, deposit = await base._seed(session)
        rows = []
        for index, charge in enumerate(("pay_stuck_1", "pay_stuck_2", "pay_stuck_3", "pay_ok")):
            row = DepositAdjustment(
                id=uuid4(),
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=appointment.id,
                kind="increase",
                status="awaiting_payment",
                old_service="Consulta",
                new_service="Avaliação",
                pending_edit={},
                diff_cents=3000,
                provider_charge_id=charge,
                expires_at=NOW - timedelta(minutes=40 - index),
            )
            session.add(row)
            rows.append(row.id)
        await session.commit()
    # Only the healthy charge answers; the others raise (provider can't tell) -> untouched.
    provider.state = {"pay_ok": ChargeState(status=PaymentStatus.PENDING)}

    for _run in range(2):
        async with db() as session:
            await service_change.expire_adjustments(session, now=NOW, limit=2)

    async with db() as session:
        statuses = {
            row.provider_charge_id: row.status
            for row in (await session.scalars(select(DepositAdjustment))).all()
        }
    assert statuses["pay_ok"] == "expired"
    assert {statuses[c] for c in ("pay_stuck_1", "pay_stuck_2", "pay_stuck_3")} == {
        "awaiting_payment"
    }


# --------------------------------------------------------------------------
# copy and buttons
# --------------------------------------------------------------------------


def test_the_agenda_reason_is_known_and_short():
    assert REASON_SERVICE_DIFF_AGENDA_PENDING in ATTENTION_REASONS
    assert len(REASON_SERVICE_DIFF_AGENDA_PENDING) <= 32
    assert "agenda" in REASON_TEXT[REASON_SERVICE_DIFF_AGENDA_PENDING].lower()


def test_the_refund_buttons_are_actions_that_fit_whatsapp():
    adjustment_id = uuid4()
    buttons = service_change.refund_question_buttons(adjustment_id)
    assert [label for _id, label in buttons] == ["Sim, quero de volta", "Não, abater no valor"]
    assert all(len(label) <= 20 for _id, label in buttons)
    decoded = [decode_action_id(button_id) for button_id, _label in buttons]
    assert decoded == [
        (service_change.ACTION_REFUND_YES, str(adjustment_id)),
        (service_change.ACTION_REFUND_NO, str(adjustment_id)),
    ]
    assert set(service_change.REFUND_ACTIONS) == {
        service_change.ACTION_REFUND_YES,
        service_change.ACTION_REFUND_NO,
    }


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("price_unavailable", "Não foi possível calcular o sinal"),
        ("no_payment_account", "Não foi possível calcular o sinal"),
        ("charge_failed", "Pix da diferença"),
        ("adjustment_pending", "aguardando pagamento"),
        ("refund_question_pending", "diferença do sinal"),
    ],
)
def test_blocked_text_never_changes_anything_and_points_to_the_clinic(reason, expected):
    text = service_change.blocked_text(reason)
    assert expected in text
    assert "continua como estava" in text


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("refund_requested", "a clínica vai entrar em contato"),
        ("kept_credit", "abatida"),
        ("already_answered", "já foi respondida"),
        ("not_found", "Não encontrei"),
    ],
)
def test_refund_answer_texts_never_promise_an_automatic_refund(outcome, expected):
    text = service_change.refund_answer_text(outcome, 3000)
    assert expected in text
    assert "automátic" not in text.lower()


# --------------------------------------------------------------------------
# The agenda read (GET /calendar/events): what the clinic holds and a pending difference
# (browser proof 2026-10-26: "Sinal pago · R$ 60,00" after a paid difference of R$ 30, and a
# change the PATIENT asked for, waiting for its Pix, invisible in the drawer)
# --------------------------------------------------------------------------


async def _seeded(db):
    async with db() as session:
        return await base._seed(session)


async def _views(db, seeded):
    from secretaria.services.payments.deposit_lifecycle import load_deposit_views

    tenant, _patient, appointment, _deposit = seeded
    async with db() as session:
        return (await load_deposit_views(session, tenant.id, [appointment.id]))[appointment.id]


async def test_the_agenda_view_of_a_plain_paid_sinal_is_unchanged(db, provider):
    seeded = await _seeded(db)

    view = await _views(db, seeded)

    assert view.amount_cents == 6000
    assert view.held_cents is None and view.pending_difference_cents is None


async def test_the_agenda_view_shows_a_difference_waiting_for_its_pix(db, provider):
    seeded = await _seeded(db)
    created = await base._request(db, seeded, "Avaliação", minutes=60)
    assert created.kind == "awaiting_payment"

    view = await _views(db, seeded)

    assert view.pending_difference_cents == 3000
    assert view.held_cents is None  # unpaid: not money held


async def test_the_agenda_view_shows_the_total_held_after_a_paid_difference(db, provider):
    seeded = await _seeded(db)
    created = await base._request(db, seeded, "Avaliação", minutes=60)
    hook_base._paid(provider, created)
    assert (await hook_base._process(db, seeded[0])).outcome == "adjustment_applied"

    view = await _views(db, seeded)

    assert view.held_cents == 9000 and view.amount_cents == 6000
    assert view.pending_difference_cents is None
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert view.held_cents == await service_change.held_deposit_cents(session, deposit)


async def test_the_agenda_view_never_reads_another_clinics_adjustment(db, provider):
    from secretaria.services.payments import adjustment_queries

    seeded = await _seeded(db)
    await base._request(db, seeded, "Avaliação", minutes=60)

    async with db() as session:
        mine = await adjustment_queries.load_adjustment_totals(
            session, seeded[0].id, [seeded[3].id]
        )
        foreign = await adjustment_queries.load_adjustment_totals(
            session, uuid4(), [seeded[3].id]
        )
        nothing = await adjustment_queries.load_adjustment_totals(session, seeded[0].id, [])
    assert mine[seeded[3].id].pending_difference_cents == 3000
    assert foreign == {} and nothing == {}


def test_the_deposit_wire_carries_the_held_total_and_the_pending_difference():
    from secretaria.api.hub.calendar import _deposit_read
    from secretaria.models.pix_deposit import PixDepositStatus
    from secretaria.services.payments.deposit_lifecycle import AppointmentDepositView

    read = _deposit_read(
        AppointmentDepositView(
            PixDepositStatus.PAID, 6000, held_cents=9000, pending_difference_cents=3000
        )
    )
    assert read.model_dump() == {
        "status": "confirmado_pago",
        "amount_cents": 6000,
        "needs_attention": False,
        "held_cents": 9000,
        "pending_difference_cents": 3000,
    }
