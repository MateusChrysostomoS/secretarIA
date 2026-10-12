"""Management displays persisted instants in the clinic's timezone without moving them."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant


def _appointment(start):
    return {
        "id": str(uuid4()),
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "appointment_type": "Consulta",
        "google_event_id": "test-event",
    }


@pytest.mark.parametrize("switch", [False, True])
@pytest.mark.parametrize(
    "start, timezone, expected",
    [
        (datetime(2026, 10, 16, 17, 40, tzinfo=UTC), "America/Sao_Paulo", "16/10/2026 às 14:40"),
        (datetime(2026, 10, 16, 17, 40), "America/Sao_Paulo", "16/10/2026 às 14:40"),
        (
            datetime(2026, 10, 16, 14, 40, tzinfo=ZoneInfo("America/Sao_Paulo")),
            "America/Sao_Paulo",
            "16/10/2026 às 14:40",
        ),
        (datetime(2026, 10, 16, 1, 10, tzinfo=UTC), "America/Sao_Paulo", "15/10/2026 às 22:10"),
        (datetime(2026, 10, 16, 17, 40, tzinfo=UTC), "UTC", "16/10/2026 às 17:40"),
        (datetime(2026, 10, 16, 17, 40, tzinfo=UTC), "America/New_York", "16/10/2026 às 13:40"),
    ],
)
async def test_manage_and_cancel_preview_use_clinic_timezone(start, timezone, expected, switch):
    tenant = _tenant()
    tenant.timezone = timezone
    tenant.initial_flows["ai_draft_v2"] = switch
    appointment = _appointment(start)
    agenda = _FakeCalendar()
    result = await fr.route(
        _conversation(flow_state=FlowState.MENU),
        tenant,
        agenda,
        fr.LABEL_MANAGE_APPOINTMENT,
        upcoming_appointments=[appointment],
    )
    assert expected in result.bubbles[0].body
    assert result.flow_managing_appointment_id == UUID(appointment["id"])
    for result in (
        await fr.enter_manage_action("cancel", tenant, [appointment]),
        await fr.route(
            _conversation(
                flow_state=FlowState.MANAGE_BOOKING,
                flow_step=fr.STEP_MANAGE_ACTION,
                flow_managing_appointment_id=appointment["id"],
            ),
            tenant,
            agenda,
            fr.LABEL_CANCEL_APPT,
            upcoming_appointments=[appointment],
        ),
    ):
        assert expected in result.bubbles[0].body
        assert result.appointment_cancel_id is None
    assert agenda.cancelled == []
    assert agenda.updated == []
    assert appointment["start_at"] == start


@pytest.mark.parametrize("intent", [None, "cancel", "reschedule"])
async def test_multiple_appointments_local_labels_keep_original_iso_ids(intent):
    tenant = _tenant()
    tenant.timezone = "America/Sao_Paulo"
    first = _appointment(datetime(2026, 10, 16, 17, 40, tzinfo=UTC))
    second = _appointment(datetime(2026, 10, 17, 1, 10, tzinfo=UTC))
    appointments = [first, second]
    if intent:
        result = await fr.enter_manage_action(intent, tenant, appointments)
    else:
        result = await fr.route(
            _conversation(flow_state=FlowState.MENU),
            tenant,
            None,
            fr.LABEL_MANAGE_APPOINTMENT,
            upcoming_appointments=appointments,
        )
    assert result.bubbles[0].rows == [
        (f"slot|{first['start_at'].isoformat()}", "16/10 14:40 Consulta"),
        (f"slot|{second['start_at'].isoformat()}", "16/10 22:10 Consulta"),
    ]
    if intent != "reschedule":
        picked = await fr.route(
            _conversation(flow_state=result.flow_state, flow_step=result.flow_step),
            tenant,
            None,
            f"16/10 14:40 Consulta ({first['start_at'].isoformat()})",
            upcoming_appointments=appointments,
        )
        assert "16/10/2026 às 14:40" in picked.bubbles[0].body
        assert str(picked.flow_managing_appointment_id) == first["id"]
        assert picked.appointment_cancel_id is None


@pytest.mark.parametrize("switch", [False, True])
@pytest.mark.parametrize(
    "timezone, expected",
    [
        ("America/Sao_Paulo", "16/10/2026 às 14:40"),
        ("America/New_York", "16/10/2026 às 13:40"),
        ("UTC", "16/10/2026 às 17:40"),
    ],
)
async def test_management_through_worker_snapshot_keeps_clinic_timezone(timezone, expected, switch):
    from secretaria.workers.shared.greeting import _flow_tenant_snapshot

    tenant = _tenant()
    tenant.timezone = timezone
    tenant.collect_insurance = False
    tenant.insurances = None
    tenant.initial_flows["ai_draft_v2"] = switch
    snapshot = _flow_tenant_snapshot(tenant, [])
    appointment = _appointment(datetime(2026, 10, 16, 17, 40, tzinfo=UTC))
    result = await fr.route(
        _conversation(flow_state=FlowState.MENU),
        snapshot,
        None,
        fr.LABEL_MANAGE_APPOINTMENT,
        upcoming_appointments=[appointment],
    )
    assert expected in result.bubbles[0].body
    preview = await fr.enter_manage_action("cancel", snapshot, [appointment])
    assert expected in preview.bubbles[0].body
    assert preview.appointment_cancel_id is None
