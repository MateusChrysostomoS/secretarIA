"""Fixed doctor emails after a committed edit; no model and no PII in queue args."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from arq import Retry
from sqlalchemy import select, update

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    FlowState,
    ProcessedEvent,
    Professional,
)
from secretaria.services import flow_router as fr
from secretaria.services.email import EmailOutcome
from secretaria.workers import tasks
from secretaria.workers.shared import appointment_edit_notification as notice
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import wire
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world
from tests.test_appointment_edit_apply import _edit, _reply
from tests.test_professional_notification import _FakeRedis, _Mailer, _summary

MAIL_A = "doctor-a@example.test"
MAIL_B = "doctor-b@example.test"


@pytest.fixture
def transport(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)
    sender = _Mailer()
    monkeypatch.setattr(notice, "send_transactional_email_result", sender)
    monkeypatch.setattr(notice, "get_entitlements", AsyncMock(return_value=_summary()))
    monkeypatch.setattr(notice, "fetch_professional_emails", AsyncMock(return_value={}))
    return sender, _FakeRedis()


async def _confirmed(db, transport, monkeypatch, **changes):  # noqa: F811
    sender, redis = transport
    world = await seed_world(
        db,
        start_at=datetime(2030, 10, 15, 18, 20, tzinfo=UTC),
        professional_name="Dra. Ana",
        google_event_id="evt-old",
    )
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(
            return_value={
                str(world.appointment.professional_id): MAIL_A,
            }
        ),
    )
    edit = _edit(
        world,
        professional_id=world.appointment.professional_id,
        old_professional_id=world.appointment.professional_id,
        **changes,
    )
    result = fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU, appointment_edit=edit)
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, redis=redis, waba_token="t"
    )
    jobs = [job for job in redis.jobs if job[0] == "send_professional_edit_notification"]
    assert len(jobs) == 1
    return world, jobs[0]


async def _deliver(job, redis=None, **ctx):
    name, args, _kwargs = job
    assert name == "send_professional_edit_notification"
    await notice.send_professional_edit_notification({"redis": redis, **ctx}, *args)


async def test_confirmed_edit_sends_fixed_changes_and_full_updated_information(
    db,  # noqa: F811
    transport,
    monkeypatch,  # noqa: F811
):
    sender, redis = transport
    world, job = await _confirmed(
        db,
        transport,
        monkeypatch,
        insurance="Unimed",
        appointment_type="Retorno",
        attendee_name="Ana Souza",
    )
    assert sender.calls == []  # asynchronous, only a committed edit was queued
    await _deliver(job, redis)
    assert sender.calls[0]["to"] == MAIL_A
    [body] = sender.bodies
    assert "A consulta de Ana Souza mudou para:" in body
    assert "• Data: 16/10/2030" in body
    assert "• Horário: 09:00" in body
    assert "• Serviço: Retorno" in body
    assert "• Convênio: Unimed" in body
    assert "• Paciente: Ana Souza" in body
    for text in (
        "Paciente: Ana Souza",
        "Agendado por: Maria",
        "Médico: Dra. Ana",
        "Serviço: Retorno",
        "Convênio: Unimed",
        "Data: 16/10/2030",
        "Horário: 09:00",
        "Término: 09:40",
        "Clínica: Clínica Olhar",
    ):
        assert text in body
    assert "+5511" not in body
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.RESCHEDULED


async def test_duplicate_delivery_is_once_but_another_confirmed_edit_can_notify_again(
    db,  # noqa: F811
    transport,
    monkeypatch,  # noqa: F811
):
    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)
    await _deliver(job, redis)
    await _deliver(job, redis)
    assert len(sender.calls) == 1
    row = await reload_appointment(db, world.appointment.id)
    later = _edit(
        world,
        time_changed=False,
        start_at=row.start_at,
        end_at=row.end_at,
        professional_id=row.professional_id,
        old_professional_id=row.professional_id,
        insurance="Outro convênio",
    )
    await tasks._apply_flow_result(
        _reply(world),
        fr.FlowRouterResult(
            action="reply",
            flow_state=FlowState.MENU,
            appointment_edit=later,
        ),
        WA_ID,
        tenant=world.tenant,
        redis=redis,
        waba_token="t",
    )
    await _deliver(redis.jobs[-1], redis)
    assert len(sender.calls) == 2
    assert "• Convênio: Outro convênio" in sender.bodies[-1]


async def test_queue_contains_references_and_changed_names_not_patient_content(
    db,  # noqa: F811
    transport,
    monkeypatch,  # noqa: F811
):
    _world, job = await _confirmed(
        db, transport, monkeypatch, attendee_name="Ana Souza", insurance="Plano privado da família"
    )
    payload = repr(job)
    for value in ("Ana Souza", "Maria", "Plano privado da família", "Dra. Ana", MAIL_A):
        assert value not in payload


@pytest.mark.parametrize("leave", [FlowState.MENU, FlowState.EDIT_BOOKING])
async def test_draft_or_discard_without_edit_never_enqueues_mail(db, transport, leave):  # noqa: F811
    sender, redis = transport
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await tasks._apply_flow_result(
        _reply(world),
        fr.FlowRouterResult(action="reply", flow_state=leave),
        WA_ID,
        tenant=world.tenant,
        redis=redis,
        waba_token="t",
    )
    assert not redis.jobs and not sender.calls


async def test_failed_edit_never_enqueues_a_doctor_email(db, transport):  # noqa: F811
    sender, redis = transport
    world = await seed_world(
        db, status=AppointmentStatus.CANCELLED, start_at=datetime.now(UTC) + timedelta(days=3)
    )
    await tasks._apply_flow_result(
        _reply(world),
        fr.FlowRouterResult(
            action="reply",
            flow_state=FlowState.MENU,
            appointment_edit=_edit(world),
        ),
        WA_ID,
        tenant=world.tenant,
        redis=redis,
        waba_token="t",
    )
    assert not [job for job in redis.jobs if job[0] == "send_professional_edit_notification"]
    assert not sender.calls


@pytest.mark.parametrize("failure", ["lookup", "smtp"])
async def test_transient_failures_retry_then_send_once(db, transport, monkeypatch, failure):  # noqa: F811
    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)
    if failure == "lookup":
        monkeypatch.setattr(notice, "fetch_professional_emails", AsyncMock(return_value=None))
    else:
        sender.outcome = EmailOutcome.SEND_FAILED
    with pytest.raises(Retry):
        await _deliver(job, redis)
    sent_before = len(sender.calls)
    sender.outcome = EmailOutcome.SENT
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(
            return_value={
                str(world.appointment.professional_id): MAIL_A,
            }
        ),
    )
    await _deliver(job, redis, job_try=2)
    await _deliver(job, redis, job_try=3)
    assert len(sender.calls) == sent_before + 1


async def test_disabled_email_does_not_retry_or_consume_a_success_claim(db, transport, monkeypatch):  # noqa: F811
    sender, redis = transport
    _world, job = await _confirmed(db, transport, monkeypatch)
    sender.outcome = EmailOutcome.DISABLED
    await _deliver(job, redis)
    async with db() as session:
        keys = list(await session.scalars(select(ProcessedEvent.event_id)))
    assert not any(key.startswith("profedit:") for key in keys)


@pytest.mark.parametrize("alter", ["stale", "cancelled", "foreign_professional", "foreign_tenant"])
async def test_stale_cancelled_or_foreign_jobs_send_nothing(db, transport, monkeypatch, alter):  # noqa: F811
    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)
    args = list(job[1])
    async with db() as session:
        if alter == "stale":
            await session.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(insurance="Correção da clínica")
            )
        elif alter == "cancelled":
            await session.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(status=AppointmentStatus.CANCELLED)
            )
        elif alter == "foreign_professional":
            await session.execute(
                update(Professional)
                .where(Professional.id == world.appointment.professional_id)
                .values(tenant_id=uuid4())
            )
        else:
            args[0] = str(uuid4())
        await session.commit()
    await _deliver((job[0], tuple(args), job[2]), redis)
    assert not sender.calls


async def test_retry_budget_stops_without_reclaiming_success(db, transport, monkeypatch):  # noqa: F811
    sender, redis = transport
    _world, job = await _confirmed(db, transport, monkeypatch)
    sender.outcome = EmailOutcome.SEND_FAILED
    await _deliver(job, redis, job_try=5)
    assert len(sender.calls) == 1


def test_email_worker_is_registered():
    from secretaria.workers.arq_worker import registered_function_names

    assert "send_professional_edit_notification" in registered_function_names()


@pytest.mark.parametrize("alter", ["cancelled", "doctor", "insurance", "clinic"])
async def test_a_change_during_address_lookup_prevents_a_stale_email(
    db,  # noqa: F811
    transport,
    monkeypatch,
    alter,  # noqa: F811
):
    from secretaria.models import Tenant

    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)

    async def delayed_lookup(_tenant_id):
        async with db() as session:
            if alter == "clinic":
                await session.execute(
                    update(Tenant).where(Tenant.id == world.tenant.id).values(is_active=False)
                )
            else:
                values = {
                    "cancelled": {"status": AppointmentStatus.CANCELLED},
                    "doctor": {"professional_id": uuid4()},
                    "insurance": {"insurance": "Correção da clínica"},
                }[alter]
                await session.execute(
                    update(Appointment)
                    .where(Appointment.id == world.appointment.id)
                    .values(**values)
                )
            await session.commit()
        return {str(world.appointment.professional_id): MAIL_A}

    monkeypatch.setattr(notice, "fetch_professional_emails", delayed_lookup)
    await _deliver(job, redis)
    assert not sender.calls


async def test_display_data_changed_during_lookup_is_loaded_for_the_fixed_email(
    db,  # noqa: F811
    transport,
    monkeypatch,  # noqa: F811
):
    from secretaria.models import Patient

    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)

    async def delayed_lookup(_tenant_id):
        async with db() as session:
            await session.execute(
                update(Patient).where(Patient.id == world.patient.id).values(name="Maria Silva")
            )
            await session.execute(
                update(Professional)
                .where(Professional.id == world.appointment.professional_id)
                .values(name="Dra. Ana Souza")
            )
            await session.commit()
        return {str(world.appointment.professional_id): MAIL_A}

    monkeypatch.setattr(notice, "fetch_professional_emails", delayed_lookup)
    await _deliver(job, redis)
    assert "Paciente: Maria Silva" in sender.bodies[0]
    assert "Médico: Dra. Ana Souza" in sender.bodies[0]


async def test_doctor_change_notifies_new_doctor_with_current_details(db, transport, monkeypatch):  # noqa: F811
    sender, redis = transport
    world, initial_job = await _confirmed(db, transport, monkeypatch)
    row = await reload_appointment(db, world.appointment.id)
    doctor = uuid4()
    async with db() as session:
        session.add(Professional(id=doctor, tenant_id=world.tenant.id, name="Dra. Beatriz"))
        await session.commit()
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(
            return_value={
                str(row.professional_id): MAIL_A,
                str(doctor): MAIL_B,
            }
        ),
    )
    edit = _edit(
        world,
        time_changed=False,
        doctor_changed=True,
        calendar_changed=False,
        start_at=row.start_at,
        end_at=row.end_at,
        insurance=row.insurance,
        professional_id=doctor,
        old_professional_id=row.professional_id,
    )
    await tasks._apply_flow_result(
        _reply(world),
        fr.FlowRouterResult(
            action="reply",
            flow_state=FlowState.MENU,
            appointment_edit=edit,
        ),
        WA_ID,
        tenant=world.tenant,
        redis=redis,
        waba_token="t",
    )
    await _deliver(initial_job, redis)
    assert not sender.calls
    await _deliver(redis.jobs[-1], redis)
    assert sender.calls[0]["to"] == MAIL_B
    assert "• Médico: Dra. Beatriz" in sender.bodies[0]
