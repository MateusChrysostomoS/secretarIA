"""TASK-086 T9 review I1: a matrix refusal at the "Confirmar" of an "Alterar dados" edit.

The clinic's staff may refuse a convênio between the card and the tap. The edit is then a
REFUSAL (the patient reads why, the agenda goes back to the committed row), never a calendar
outage: no "agenda indisponível", no human handover, no false calendar alert to the clinic.
"""

from __future__ import annotations

import os

os.environ.setdefault("BOT_ALLOWLIST_WA_IDS", "")

from datetime import UTC, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.models import Service, Tenant, TenantInsurancePlan  # noqa: E402
from secretaria.models.conversation import FlowState  # noqa: E402
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.services import appointment_edit as ae, flow_router as fr  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import flow_runner  # noqa: E402
from secretaria.workers.shared.handover import CALENDAR_UNAVAILABLE_MESSAGE  # noqa: E402
from tests._edit_flow_support import EditCalendar  # noqa: E402
from tests._reminder_fixtures import db  # noqa: F401, E402
from tests._reminders_r3 import consent, sent, wire  # noqa: E402
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world  # noqa: E402


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] in ("text", "buttons", "list")]


async def _world(db):  # noqa: F811
    world = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old"
    )
    await consent(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.insurance_mode = "shared"
        service = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
        amil = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Amil")
        session.add_all([service, amil])
        await session.flush()
        # The clinic refused Amil for the Consulta AFTER the patient saw the card.
        session.add(
            InsuranceAcceptanceException(
                tenant_id=tenant.id,
                professional_id=None,
                service_id=service.id,
                unit_id=None,
                tenant_plan_id=amil.id,
                professional_plan_id=None,
            )
        )
        await session.commit()
        world.tenant = tenant
        world.service_id = service.id
    return world


def _edit(world, calendar) -> dict:
    start = world.start_at
    return {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-old",
        "google_event_id": "evt-old",
        "google_event_link": None,
        "appointment_type": "Consulta",
        "professional_id": None,
        "old_professional_id": None,
        "old_google_calendar_source": None,
        "insurance": "Amil",
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
        "calendar": calendar,
        "patient_name": "Maria",
        "calendar_deferred": False,
        "service_id": world.service_id,
    }


async def test_a_matrix_refusal_at_confirm_is_a_refusal_not_a_calendar_outage(
    db,  # noqa: F811
    monkeypatch,
):
    world = await _world(db)
    handoffs: list = []
    outages: list = []

    async def _human(*args, **kwargs):
        handoffs.append(args)

    async def _outage(*args, **kwargs):
        outages.append(args)

    monkeypatch.setattr(flow_runner, "_set_conversation_human_active", _human)
    monkeypatch.setattr(flow_runner, "_handle_calendar_unavailable", _outage)
    compensated: list[dict] = []
    real_compensate = flow_runner.compensate_appointment_edit

    async def _compensate(tenant, patient_id, edit):
        compensated.append(edit)
        await real_compensate(tenant, patient_id, edit)

    monkeypatch.setattr(flow_runner, "compensate_appointment_edit", _compensate)

    calendar = EditCalendar()
    edit = _edit(world, calendar)
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\nConvênio: Amil"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )

    await tasks._apply_flow_result(reply, result, WA_ID, tenant=world.tenant, waba_token="t")

    # Nothing written: the consultation keeps its convênio.
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance != "Amil"
    # The patient is told why, with the menu - never "agenda indisponível", never "Pronto".
    texts = _texts()
    assert texts[0] == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert CALENDAR_UNAVAILABLE_MESSAGE not in texts
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    # No human handover and no calendar alert; the agenda goes back to the committed row.
    assert handoffs == [] and outages == []
    assert compensated == [edit]
