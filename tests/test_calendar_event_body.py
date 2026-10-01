"""Corpo do evento: descrição com convênio e lembretes explícitos (MVP Portal, Task 6)."""

import os
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.services.calendar import (  # noqa: E402
    DEFAULT_EVENT_REMINDERS,
    CalendarService,
    build_event_description,
)


def test_description_names_service_insurance_and_channel():
    text = build_event_description(
        service="Primeira Consulta", insurance="Unimed", channel="brain_message"
    )
    assert "Serviço: Primeira Consulta" in text
    assert "Convênio: Unimed" in text
    assert "Portal" in text


def test_description_says_when_no_insurance_was_given():
    assert "Convênio: não informado" in build_event_description(service="X", insurance=None)


def test_description_marks_a_third_party_booking():
    text = build_event_description(service="X", insurance="Particular", attendee_name="Maria")
    assert "Consulta para: Maria" in text


def _fake_service(captured):
    fake = MagicMock()

    def _insert(calendarId, body):  # noqa: N803
        captured["body"] = body
        request = MagicMock()
        request.execute.return_value = {"id": "evt1", "status": "confirmed"}
        return request

    fake.events.return_value.insert.side_effect = _insert
    return fake


async def test_create_event_sends_explicit_reminders(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(CalendarService, "_client", lambda self: _fake_service(captured))
    tz = ZoneInfo("America/Sao_Paulo")
    cal = CalendarService()
    await cal.create_event(
        start=datetime(2026, 10, 5, 14, 0, tzinfo=tz),
        end=datetime(2026, 10, 5, 14, 30, tzinfo=tz),
        summary="Consulta - Maria",
        description="Convênio: Unimed",
    )
    assert captured["body"]["description"] == "Convênio: Unimed"
    assert captured["body"]["reminders"] == {
        "useDefault": False,
        "overrides": DEFAULT_EVENT_REMINDERS,
    }
    methods = {r["method"] for r in DEFAULT_EVENT_REMINDERS}
    assert methods == {"popup", "email"}


async def test_flow_confirmation_includes_insurance_in_event():
    from secretaria.models import FlowState
    from secretaria.services.flow_router import STEP_AWAITING_CONFIRMATION, route
    from tests.test_flow_router import _conversation, _FakeCalendar, _tenant

    class CapturingCalendar(_FakeCalendar):
        async def create_event(self, start, end, summary, description="", reminders=None):
            self.description = description
            return await super().create_event(start, end, summary, description)

    cal = CapturingCalendar()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_insurance="Unimed",
        flow_selected_slot="2026-10-05T14:00",
    )
    result = await route(conv, _tenant(), cal, "Confirmar", patient_name="Maria")
    assert result.appointment is not None
    assert "Convênio: Unimed" in cal.description
    assert "Serviço: Primeira Consulta" in cal.description


async def test_custom_reminders_override_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(CalendarService, "_client", lambda self: _fake_service(captured))
    tz = ZoneInfo("America/Sao_Paulo")
    await CalendarService().create_event(
        datetime(2026, 10, 5, 14, tzinfo=tz),
        datetime(2026, 10, 5, 15, tzinfo=tz),
        "Consulta", reminders=[],
    )
    assert captured["body"]["reminders"] == {"useDefault": False, "overrides": []}


async def test_unit_booking_adds_insurance_and_keeps_extra_description(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from secretaria.plugins import multi_unit

    captured = {}

    class Calendar:
        tzinfo = ZoneInfo("America/Sao_Paulo")

        async def create_event(self, **kwargs):
            captured.update(kwargs)
            return {"id": "evt1", "status": "confirmed"}

    async def resolve(*args):
        return SimpleNamespace(id=uuid4()), None

    async def insurance():
        return "Unimed"

    async def attendee():
        return None

    async def persist(*args, **kwargs):
        pass

    monkeypatch.setattr(multi_unit, "_blocked_tenant_level", lambda *args: None)
    monkeypatch.setattr(multi_unit, "_resolve_unit_or_error", resolve)
    monkeypatch.setattr(multi_unit, "_canonical_appointment_type", lambda *args: ("Consulta", None))
    monkeypatch.setattr(multi_unit, "_get_calendar", lambda: Calendar())
    monkeypatch.setattr(multi_unit, "_persist_appointment", persist)
    # The conversation is outside the tool's I/O boundary in this test.
    monkeypatch.setattr(multi_unit, "_conversation_insurance", insurance, raising=False)
    monkeypatch.setattr(multi_unit, "_conversation_attendee_name", attendee, raising=False)
    token = multi_unit._tenant_id_ctx.set(uuid4())
    try:
        result = await multi_unit.create_event_at_unit.ainvoke({
            "unit_name": "Centro", "start": "2026-10-05T14:00:00-03:00",
            "end": "2026-10-05T14:30:00-03:00", "summary": "Consulta",
            "description": "Nota adicional", "appointment_type": "Consulta",
        })
    finally:
        multi_unit._tenant_id_ctx.reset(token)
    assert result["id"] == "evt1"
    assert "Conv\u00eanio: Unimed" in captured["description"]
    assert "Servi\u00e7o: Consulta" in captured["description"]
    assert captured["description"].endswith("Nota adicional")


async def test_create_event_success_log_omits_patient_title(monkeypatch):
    from secretaria.services import calendar

    captured = {}
    records = []

    class CapturingLogger:
        def info(self, event, **fields):
            records.append((event, fields))

    monkeypatch.setattr(CalendarService, "_client", lambda self: _fake_service(captured))
    monkeypatch.setattr(calendar, "logger", CapturingLogger())
    tz = ZoneInfo("America/Sao_Paulo")
    await CalendarService().create_event(
        start=datetime(2026, 10, 5, 14, tzinfo=tz),
        end=datetime(2026, 10, 5, 15, tzinfo=tz),
        summary="Consulta - Patient Private Name",
        description="Consulta para: Attendee Private Name",
    )
    assert records == [("calendar_event_created", {"event_id": "evt1"})]
    assert "Private Name" not in repr(records)
