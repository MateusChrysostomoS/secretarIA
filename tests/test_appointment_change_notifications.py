"""A7: the PATIENT cancelled or moved - the clinic, the doctor and the Portal patient hear it.

Live 2026-10-05: booking mailed three people, the patient's own "Cancelar"/"Remarcar"
(and the agent's tools) mailed nobody. Two layers here:

  * the job (`plugins/appointment_change_notifications.py::notify_appointment_change`)
    on a real in-memory DB, with SMTP / brain-api / entitlements faked - who gets what,
    in the clinic's timezone, exactly once, with a real retry on a transient failure;
  * every patient path enqueues it after its commit (flow cancel and move, "Alterar
    Dados", the reminder buttons, the agent's `cancel_event`, "Marcar outra") - and the
    hub, where STAFF act (TASK-044 tells the patient there), never does.
"""

import ast
import os
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from arq import Retry  # noqa: E402

import secretaria  # noqa: E402
from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.config import get_settings  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    Professional,
    Tenant,
)
from secretaria.plugins import (  # noqa: E402
    appointment_change_notifications as acn,
    booking_notifications as bn,
)
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.appointment_replacement import ReplacedAppointment  # noqa: E402
from secretaria.services.brain_patients import PatientEmailResult  # noqa: E402
from secretaria.services.email import _TEMPLATES, EmailOutcome, _SafeDict  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import replacement  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401
from tests._reminders_r3 import wire  # noqa: E402
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world  # noqa: E402

CLINIC_EMAIL = "clinica@clinica.example"
DOCTOR_EMAIL = "ana@clinica.example"
PATIENT_EMAIL = "maria@paciente.example"
# 17:00 UTC is 14:00 in the clinic (America/Sao_Paulo) - the hour a mail must show.
START = datetime(2030, 10, 15, 17, 0, tzinfo=UTC)
MOVED_TO = datetime(2030, 10, 16, 13, 0, tzinfo=UTC)


class _Pool:
    """The arq pool: records every enqueue."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))

    def named(self, name: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for job, args, kwargs in self.jobs if job == name]


class _Mailer:
    """Records every send; `fail` names templates that fail transiently ONCE."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = set(fail)

    async def __call__(self, *, to, template, variables):
        if template in self.fail:
            self.fail.discard(template)
            return EmailOutcome.SEND_FAILED
        self.calls.append((to, template, variables))
        return EmailOutcome.SENT

    def body(self, template: str) -> str:
        [variables] = [v for _to, t, v in self.calls if t == template]
        tpl = _TEMPLATES[template]
        safe = _SafeDict(variables)
        return f"{tpl.subject.format_map(safe)}\n{tpl.body.format_map(safe)}"

    def sent(self) -> set[tuple[str, str]]:
        return {(to, template) for to, template, _v in self.calls}


@pytest.fixture
def world(monkeypatch, db):  # noqa: F811
    """The job's collaborators: DB, SMTP, brain-api, entitlements, the agenda URL."""
    mailer = _Mailer()
    lookups = SimpleNamespace(professional=0, active=True, emails={})
    monkeypatch.setattr(acn, "async_session_factory", db)
    monkeypatch.setattr(bn, "async_session_factory", db)
    monkeypatch.setattr(acn, "send_transactional_email_result", mailer)
    monkeypatch.setattr(
        get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "https://hub.example.com", raising=False
    )

    async def _patient_email(_tenant_id, _external_id):
        return PatientEmailResult(available=True, email=PATIENT_EMAIL)

    async def _professional_emails(_tenant_id):
        lookups.professional += 1
        return lookups.emails

    async def _entitlements(_tenant_id, _redis):
        return SimpleNamespace(active=lookups.active)

    monkeypatch.setattr(acn, "fetch_patient_email_result", _patient_email)
    monkeypatch.setattr(acn, "fetch_professional_emails", _professional_emails)
    monkeypatch.setattr(acn, "get_entitlements", _entitlements)
    return SimpleNamespace(db=db, mailer=mailer, lookups=lookups)


async def _rows(
    world,
    *,
    channel: str = "brain_message",
    status: AppointmentStatus = AppointmentStatus.CANCELLED,
    start_at: datetime = START,
):
    async with world.db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Olhar",
            contact_email=CLINIC_EMAIL,
            timezone="America/Sao_Paulo",
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana")
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Maria Souza",
            channel=channel,
            wa_id=None if channel == "brain_message" else WA_ID,
            external_id=str(uuid4()) if channel == "brain_message" else None,
        )
        session.add_all([doctor, patient])
        await session.flush()
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            professional_id=doctor.id,
            google_event_id="evt-1",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
        )
        session.add(appointment)
        await session.commit()
    world.lookups.emails = {str(doctor.id): DOCTOR_EMAIL}
    return tenant, appointment


async def _run(appointment, change, *, start_iso="", previous_iso="", ctx=None):
    await acn.notify_appointment_change(
        ctx or {},
        str(appointment.tenant_id),
        str(appointment.id),
        change,
        start_iso,
        previous_iso,
    )


async def _run_move(appointment, ctx=None):
    await _run(
        appointment,
        acn.CHANGE_RESCHEDULED,
        start_iso=MOVED_TO.isoformat(),
        previous_iso=START.isoformat(),
        ctx=ctx,
    )


# --- The job: who hears what -------------------------------------------------------------


async def test_a_cancellation_mails_the_clinic_the_doctor_and_the_portal_patient(world):
    _tenant, appointment = await _rows(world)

    await _run(appointment, acn.CHANGE_CANCELLED)

    assert world.mailer.sent() == {
        (CLINIC_EMAIL, "appointment_cancelled_clinic"),
        (DOCTOR_EMAIL, "appointment_cancelled_professional"),
        (PATIENT_EMAIL, "appointment_cancelled_patient"),
    }
    clinic = world.mailer.body("appointment_cancelled_clinic")
    assert clinic.startswith("Consulta cancelada — 15/10/2030 às 14:00\n")
    assert "Profissional: Dra. Ana\nServiço: Consulta\nQuando: 15/10/2030 às 14:00\n" in clinic
    assert "Ver na agenda:\nhttps://hub.example.com/agenda\n" in clinic
    # The id only as the footer; no patient name on the clinic's record.
    assert clinic.rstrip().splitlines()[-1] == f"Ref.: {appointment.id}"
    assert clinic.count(str(appointment.id)) == 1
    assert "Maria" not in clinic
    doctor = world.mailer.body("appointment_cancelled_professional")
    assert "Olá, Dra. Ana!" in doctor and "Paciente: Maria Souza\n" in doctor
    assert str(appointment.id) not in doctor
    patient = world.mailer.body("appointment_cancelled_patient")
    assert "Quando: 15/10/2030 às 14:00\n" in patient
    assert "hub.example.com" not in patient and str(appointment.id) not in patient


async def test_a_move_mails_the_clinic_and_the_portal_patient_but_never_the_doctor(world):
    """The doctor already gets "Consulta alterada" from the edit outbox for this move."""
    _tenant, appointment = await _rows(
        world, status=AppointmentStatus.RESCHEDULED, start_at=MOVED_TO
    )

    await _run_move(appointment)

    assert world.mailer.sent() == {
        (CLINIC_EMAIL, "appointment_rescheduled_clinic"),
        (PATIENT_EMAIL, "appointment_rescheduled_patient"),
    }
    assert world.lookups.professional == 0
    clinic = world.mailer.body("appointment_rescheduled_clinic")
    assert clinic.startswith("Consulta remarcada — 16/10/2030 às 10:00\n")
    assert "Novo horário: 16/10/2030 às 10:00\nHorário anterior: 15/10/2030 às 14:00\n" in clinic
    assert "Profissional: Dra. Ana\n" in clinic
    patient = world.mailer.body("appointment_rescheduled_patient")
    assert "Horário anterior: 15/10/2030 às 14:00\n" in patient
    assert "Adicionar à sua agenda:\nhttps://calendar.google.com/" in patient
    assert "hub.example.com" not in patient


async def test_a_whatsapp_patient_gets_no_mail_of_their_own(world):
    _tenant, appointment = await _rows(world, channel="whatsapp")

    await _run(appointment, acn.CHANGE_CANCELLED)

    assert world.mailer.sent() == {
        (CLINIC_EMAIL, "appointment_cancelled_clinic"),
        (DOCTOR_EMAIL, "appointment_cancelled_professional"),
    }


async def test_a_doctor_without_an_address_costs_nobody_else_their_mail(world):
    _tenant, appointment = await _rows(world)
    world.lookups.emails = {}

    await _run(appointment, acn.CHANGE_CANCELLED)

    assert {t for _to, t in world.mailer.sent()} == {
        "appointment_cancelled_clinic",
        "appointment_cancelled_patient",
    }


# --- Exactly once, and a retry that really runs ------------------------------------------


async def test_a_second_run_of_the_same_change_sends_nothing_again(world):
    _tenant, appointment = await _rows(world)

    await _run(appointment, acn.CHANGE_CANCELLED)
    await _run(appointment, acn.CHANGE_CANCELLED)

    assert len(world.mailer.calls) == 3


async def test_a_transient_failure_retries_and_resends_only_the_missing_mail(world):
    _tenant, appointment = await _rows(world)
    world.mailer.fail = {"appointment_cancelled_patient"}

    with pytest.raises(Retry):
        await _run(appointment, acn.CHANGE_CANCELLED)
    assert {t for _to, t in world.mailer.sent()} == {
        "appointment_cancelled_clinic",
        "appointment_cancelled_professional",
    }

    await _run(appointment, acn.CHANGE_CANCELLED, ctx={"job_try": 2})
    assert len(world.mailer.calls) == 3
    assert (PATIENT_EMAIL, "appointment_cancelled_patient") in world.mailer.sent()


async def test_a_doctor_lookup_outage_is_retried_and_the_budget_ends_it(world):
    _tenant, appointment = await _rows(world)
    world.lookups.emails = None  # brain-api could not answer

    with pytest.raises(Retry):
        await _run(appointment, acn.CHANGE_CANCELLED)
    # Out of attempts: logged and stopped, never an endless queue.
    await _run(appointment, acn.CHANGE_CANCELLED, ctx={"job_try": 5})

    assert [t for _to, t, _v in world.mailer.calls].count("appointment_cancelled_clinic") == 1


async def test_a_move_after_the_announced_one_is_its_own_mail_not_a_repeat(world):
    _tenant, appointment = await _rows(
        world, status=AppointmentStatus.RESCHEDULED, start_at=MOVED_TO
    )
    await _run_move(appointment)
    await _run_move(appointment)
    assert len(world.mailer.calls) == 2


# --- Never about a change that is no longer true ------------------------------------------


async def test_a_cancellation_of_a_row_that_is_live_again_is_not_announced(world):
    _tenant, appointment = await _rows(world, status=AppointmentStatus.SCHEDULED)
    await _run(appointment, acn.CHANGE_CANCELLED)
    assert world.mailer.calls == []


async def test_a_move_superseded_by_a_later_one_is_not_announced(world):
    _tenant, appointment = await _rows(
        world, status=AppointmentStatus.RESCHEDULED, start_at=MOVED_TO + timedelta(days=1)
    )
    await _run_move(appointment)
    assert world.mailer.calls == []


async def test_a_lapsed_clinic_sends_nothing(world):
    _tenant, appointment = await _rows(world)
    world.lookups.active = False
    await _run(appointment, acn.CHANGE_CANCELLED)  # no Retry either
    assert world.mailer.calls == []


async def test_another_clinics_appointment_is_never_mailed(world):
    _tenant, appointment = await _rows(world)
    await acn.notify_appointment_change({}, str(uuid4()), str(appointment.id), acn.CHANGE_CANCELLED)
    assert world.mailer.calls == []


# --- The enqueue --------------------------------------------------------------------------


async def test_the_job_id_names_the_change_and_one_instant_has_one_spelling():
    pool, appointment_id, tenant_id = _Pool(), uuid4(), uuid4()
    local = ZoneInfo("America/Sao_Paulo")

    await acn.enqueue_appointment_change_notification(
        pool, tenant_id, appointment_id, acn.CHANGE_CANCELLED
    )
    await acn.enqueue_appointment_change_notification(
        pool,
        tenant_id,
        appointment_id,
        acn.CHANGE_RESCHEDULED,
        # The flow hands over clinic-local times, the edit path UTC: same key either way.
        new_start_at=MOVED_TO.astimezone(local),
        previous_start_at=START.replace(tzinfo=None),
    )

    [(cancel_args, cancel_kw), (move_args, move_kw)] = pool.named(acn.JOB_NAME)
    assert cancel_args == (str(tenant_id), str(appointment_id), "cancelled", "", "")
    assert cancel_kw == {"_job_id": f"apptchange:cancelled:{appointment_id}"}
    assert move_args[3:] == (MOVED_TO.isoformat(), START.isoformat())
    assert move_kw == {
        "_job_id": f"apptchange:rescheduled:{appointment_id}:{START.isoformat()}"
        f":{MOVED_TO.isoformat()}"
    }


async def test_the_enqueue_never_raises_and_a_non_move_is_not_queued():
    class _Down:
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("redis down")

    await acn.enqueue_appointment_change_notification(None, uuid4(), uuid4(), "cancelled")
    await acn.enqueue_appointment_change_notification(_Down(), uuid4(), uuid4(), "cancelled")
    pool = _Pool()
    await acn.enqueue_appointment_change_notification(
        pool,
        uuid4(),
        uuid4(),
        acn.CHANGE_RESCHEDULED,
        new_start_at=START,
        previous_start_at=START,
    )
    assert pool.jobs == []


# --- Every patient path enqueues it, after its commit ------------------------------------


@pytest.fixture
def worker(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _reply(world_rows):
    return tasks._ReplyContext(
        conversation_id=world_rows.conversation.id, patient_ref=WA_ID, inbound_body="Sim"
    )


def _change_jobs(pool) -> list[tuple]:
    return [args for args, _kw in pool.named(acn.JOB_NAME)]


async def test_the_flow_cancel_enqueues_the_cancellation_once(db, worker):  # noqa: F811
    rows = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old"
    )
    pool = _Pool()
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.TextBubble(body="Pronto! Sua consulta foi cancelada. ✅")],
        flow_state=fr.FlowState.IDLE,
        appointment_cancel_id="evt-old",
    )

    await tasks._apply_flow_result(
        _reply(rows), result, WA_ID, redis=pool, tenant=rows.tenant, waba_token="t"
    )
    # A replay of the same turn: the row is already CANCELLED, nothing to announce.
    await tasks._apply_flow_result(
        _reply(rows), result, WA_ID, redis=pool, tenant=rows.tenant, waba_token="t"
    )

    assert _change_jobs(pool) == [
        (str(rows.tenant.id), str(rows.appointment.id), "cancelled", "", "")
    ]


async def test_the_flow_move_enqueues_the_move_and_the_doctor_keeps_their_own_mail(
    db,  # noqa: F811
    worker,
):
    start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    new_start = start + timedelta(days=1)
    rows = await seed_world(
        db, start_at=start, google_event_id="evt-old", professional_name="Dra. Ana"
    )
    pool = _Pool()
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.TextBubble(body="Pronto! Sua consulta foi remarcada. ✅")],
        flow_state=fr.FlowState.IDLE,
        appointment_reschedule={
            "google_event_id": "evt-old",
            "start_at": new_start,
            "end_at": new_start + timedelta(minutes=30),
        },
    )

    await tasks._apply_flow_result(
        _reply(rows), result, WA_ID, redis=pool, tenant=rows.tenant, waba_token="t"
    )

    assert _change_jobs(pool) == [
        (
            str(rows.tenant.id),
            str(rows.appointment.id),
            "rescheduled",
            new_start.isoformat(),
            start.isoformat(),
        )
    ]
    # The doctor's "Consulta alterada" - the reason this module mails them nothing.
    assert len(pool.named("send_professional_edit_notification")) == 1


async def test_alterar_dados_enqueues_a_move_only_when_the_time_changed(db, worker):  # noqa: F811
    start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    rows = await seed_world(db, start_at=start, google_event_id="evt-old")
    pool = _Pool()

    def _edit(**changes) -> dict:
        new_start = changes.pop("start_at", start)
        return {
            "appointment_id": rows.appointment.id,
            "old_google_event_id": "evt-old",
            "google_event_id": "evt-old",
            "google_event_link": None,
            "appointment_type": "Consulta",
            "professional_id": None,
            "old_professional_id": None,
            "insurance": rows.appointment.insurance,
            "attendee_name": None,
            "start_at": new_start,
            "end_at": new_start + timedelta(minutes=30),
            "time_changed": False,
            "doctor_changed": False,
            **changes,
        }

    async def _apply(edit):
        result = fr.FlowRouterResult(
            action="reply",
            bubbles=[fr.MenuBubble(body="menu", labels=["Agendar", "Outro"])],
            flow_state=fr.FlowState.MENU,
            appointment_edit=edit,
        )
        await tasks._apply_flow_result(
            _reply(rows), result, WA_ID, redis=pool, tenant=rows.tenant, waba_token="t"
        )

    await _apply(_edit(insurance="Amil"))
    assert _change_jobs(pool) == []

    await _apply(_edit(start_at=start + timedelta(days=2), time_changed=True))
    assert (await reload_appointment(db, rows.appointment.id)).status == (
        AppointmentStatus.RESCHEDULED
    )
    assert _change_jobs(pool) == [
        (
            str(rows.tenant.id),
            str(rows.appointment.id),
            "rescheduled",
            (start + timedelta(days=2)).isoformat(),
            start.isoformat(),
        )
    ]


async def test_the_reminder_cancel_button_enqueues_once(db, worker):  # noqa: F811
    rows = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    pool = _Pool()

    for _tap in range(2):  # the second tap is on an already cancelled booking
        await tasks._handle_action_button(
            _reply(rows), "apptcancelyes", str(rows.appointment.id), redis=pool
        )

    assert _change_jobs(pool) == [
        (str(rows.tenant.id), str(rows.appointment.id), "cancelled", "", "")
    ]


async def test_the_agents_cancel_event_enqueues_the_cancellation(db, worker):  # noqa: F811
    rows = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-agent"
    )
    pool = _Pool()
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(rows.tenant.id)),
        (ai_tools._redis_ctx, ai_tools._redis_ctx.set(pool)),
    ]
    try:
        await ai_tools._mark_appointment_cancelled("evt-agent")
        await ai_tools._mark_appointment_cancelled("evt-agent")  # already cancelled
    finally:
        for var, token in reversed(tokens):
            var.reset(token)

    assert _change_jobs(pool) == [
        (str(rows.tenant.id), str(rows.appointment.id), "cancelled", "", "")
    ]


async def test_marcar_outra_announces_the_cancelled_original(db, worker):  # noqa: F811
    rows = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    pool = _Pool()
    replaced = ReplacedAppointment(
        appointment_id=rows.appointment.id,
        google_event_id=None,
        professional_id=None,
        start_at=rows.start_at,
        money_note=None,
    )

    await replacement._finish_replacement(rows.tenant, replaced, redis=pool)

    assert _change_jobs(pool) == [
        (str(rows.tenant.id), str(rows.appointment.id), "cancelled", "", "")
    ]


def test_the_hub_never_enqueues_it_staff_actions_are_told_by_task_044():
    """A staff cancel/edit already tells the patient (TASK-044); announcing it here too
    would be the double notice. Nothing under api/ may reach this module."""
    api = Path(secretaria.__file__).parent / "api"
    offenders = []
    for path in api.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("appointment_change_notifications" in name for name in names):
                offenders.append(path.name)
    assert offenders == []


def test_every_template_is_registered():
    assert set(acn.TEMPLATES.values()) <= set(_TEMPLATES)
